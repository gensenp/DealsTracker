"""Alembic environment.

Deliberately takes both its connection string and its metadata from db.py
rather than from alembic.ini. One source of truth for each: DATABASE_URL
already decides which database the app talks to, including the `postgres://`
to psycopg 3 rewrite that Supabase's URLs need, and db.Base.metadata is
already the definition of the schema. Repeating either in alembic.ini would
let a migration run against a different database from the one the app uses.
"""
import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# migrations/ sits beside db.py, which is imported as a top-level module.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# What `alembic revision --autogenerate` compares the database against.
target_metadata = db.Base.metadata

# Escaped because ConfigParser reads % as interpolation, and a Supabase
# password can legitimately contain one.
config.set_main_option("sqlalchemy.url",
                       db.get_database_url().replace("%", "%%"))


def run_migrations_offline() -> None:
    """Emit the SQL instead of running it.

    `alembic upgrade head --sql` is the way to hand statements to someone
    whose database is behind a change process you don't control.
    """
    context.configure(
        url=db.get_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
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
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # Without this a type change is invisible to autogenerate, so
            # widening a String(255) in the models would produce an empty
            # migration that looks as though it had been applied.
            compare_type=True,
            # SQLite cannot ALTER a column in place. Batch mode rebuilds the
            # table instead, so a local checkout can run the same migrations
            # as the deployment rather than a separate dialect of them.
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
