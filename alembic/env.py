"""Alembic environment. The DB URL comes from app settings (env/.env), never
from alembic.ini — one source of truth for configuration."""

from docfactory_core.config import get_settings
from docfactory_core.models import Base
from sqlalchemy import engine_from_config, pool

from alembic import context

config = context.config
# Migrations run as the owner: they create tables, roles and RLS policies,
# none of which the restricted application role is permitted to do.
config.set_main_option("sqlalchemy.url", get_settings().database_admin_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a DB connection (--sql mode)."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
