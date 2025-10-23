from __future__ import annotations

import asyncio
from logging.config import fileConfig

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from alembic import context
from mcp_agent_mail.config import get_settings
from mcp_agent_mail.models import SQLModel

config = context.config
fileConfig(config.config_file_name)


def get_url() -> str:
    return get_settings().database.url


def run_migrations_offline() -> None:
    url = get_url()
    context.configure(url=url, target_metadata=SQLModel.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online_async() -> None:
    connectable: AsyncEngine = create_async_engine(get_url(), pool_pre_ping=True)

    async with connectable.connect() as connection:
        def _configure(connection_sync):
            context.configure(connection=connection_sync, target_metadata=SQLModel.metadata)

        await connection.run_sync(_configure)
        await connection.run_sync(lambda conn: context.begin_transaction())
        await connection.run_sync(lambda conn: context.run_migrations())

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online_async())


