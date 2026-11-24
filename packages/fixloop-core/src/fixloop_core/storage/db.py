"""Database engine and session factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from fixloop_core.settings import DatabaseSettings


class Database:
    def __init__(self, settings: DatabaseSettings) -> None:
        self.engine: AsyncEngine = create_async_engine(
            settings.url,
            pool_size=settings.pool_size,
            max_overflow=settings.pool_size,
            echo=settings.echo,
            pool_pre_ping=True,
            # API queries must never wait forever: a 5 s statement timeout per connection.
            connect_args={"server_settings": {"statement_timeout": "15000", "application_name": "fixloop"}}
            if settings.url.startswith("postgresql+asyncpg")
            else {},
        )
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessions() as session:
            yield session

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        async with self.sessions() as session, session.begin():
            yield session

    async def ping(self) -> bool:
        from sqlalchemy import text

        async with self.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True

    async def close(self) -> None:
        await self.engine.dispose()
