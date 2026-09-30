import asyncio
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy import text

from app.db import make_engine, make_session_factory
from app.dbtypes import Money
from app.main import run_migrations


def _url(tmp_path, name="db.sqlite") -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / name).as_posix()}"


async def test_session_against_temporary_database(tmp_path):
    engine = make_engine(_url(tmp_path))
    async with make_session_factory(engine)() as session:
        assert await session.scalar(text("SELECT 1")) == 1
        assert (await session.scalar(text("PRAGMA journal_mode"))).lower() == "wal"
        assert await session.scalar(text("PRAGMA busy_timeout")) == 5000
        assert await session.scalar(text("PRAGMA foreign_keys")) == 1
    await engine.dispose()


async def test_decimal_column_round_trips_exactly(tmp_path):
    metadata = sa.MetaData()
    table = sa.Table("amounts", metadata, sa.Column("id", sa.Integer, primary_key=True),
                     sa.Column("amount", Money))  # fmt: skip
    engine = make_engine(_url(tmp_path))
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
        total = Decimal("0.1") + Decimal("0.2")
        tiny = Decimal("1234567890.123456789012345678")
        await connection.execute(table.insert(), [{"amount": total}, {"amount": tiny}])
        stored = (await connection.execute(sa.select(table.c.amount).order_by(table.c.id))).all()
    await engine.dispose()

    assert [row.amount for row in stored] == [Decimal("0.3"), tiny]
    assert all(isinstance(row.amount, Decimal) for row in stored)


async def test_migrations_upgrade_to_the_full_schema(tmp_path):
    url = _url(tmp_path, "migrated.sqlite")
    await asyncio.to_thread(run_migrations, url)

    engine = make_engine(url)
    async with engine.connect() as connection:
        tables = await connection.run_sync(lambda sync: sa.inspect(sync).get_table_names())
    await engine.dispose()

    expected = {
        "users", "sessions", "login_attempts", "assets", "asset_source_symbols",
        "fee_profiles", "portfolio_fee_profiles", "portfolios", "portfolio_assets",
        "positions", "value_snapshots", "orders", "trades",
    }  # fmt: skip
    assert expected <= set(tables)


async def test_health_through_the_client(anonymous):
    response = await anonymous.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
