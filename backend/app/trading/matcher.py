"""Background evaluation of open orders and the daily value snapshot."""

import asyncio
import logging
import os
from datetime import UTC, date, datetime, time
from pathlib import Path

from sqlalchemy import select

from app.db import SessionFactory
from app.marketdata.service import MarketService
from app.models import Order, Portfolio, ValueSnapshot
from app.trading.orders import evaluate
from app.trading.valuation import value_portfolio

log = logging.getLogger(__name__)

# The quote cache decides how often a source is really asked (5 s for crypto,
# 60 s otherwise); ticking every 5 s keeps both within their evaluation limits.
TICK_SECONDS = 5.0


class AlreadyRunning(RuntimeError):
    pass


class ProcessLock:
    """Advisory lock file that lets only one backend process run the matcher."""

    def __init__(self, path: str):
        self.path = Path(path)
        self._handle = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+")  # noqa: SIM115 - held for the process lifetime
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise AlreadyRunning(
                f"Another backend process holds {self.path}. Only one may run at a time."
            ) from exc
        self._handle = handle

    def release(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None


class Matcher:
    def __init__(self, session_factory: SessionFactory, market: MarketService):
        self.session_factory = session_factory
        self.market = market
        self._task: asyncio.Task | None = None
        self._snapshot_day: date | None = None

    async def tick(self) -> int:
        """Evaluate every open order once. Returns the number of fills."""
        async with self.session_factory() as session:
            order_ids = list(
                await session.scalars(
                    select(Order.id).where(Order.status == "open").order_by(Order.id)
                )
            )
        fills = 0
        for order_id in order_ids:
            try:
                result = await evaluate(
                    self.session_factory, self.market, order_id, immediate=False
                )
            except Exception:
                log.exception("evaluating order %s failed", order_id)
                continue
            if result is not None and result.status == "filled":
                fills += 1
        return fills

    async def daily_snapshots(self) -> int:
        """Record today's value for every portfolio that has no daily point yet."""
        now = self.market.now()
        day_start = datetime.combine(now.date(), time.min, tzinfo=UTC)
        written = 0
        async with self.session_factory() as session:
            done = set(
                await session.scalars(
                    select(ValueSnapshot.portfolio_id).where(
                        ValueSnapshot.kind == "daily", ValueSnapshot.at >= day_start
                    )
                )
            )
            portfolios = (await session.scalars(select(Portfolio))).all()
            # Value everything first: quotes are fetched over the network, and no
            # write transaction may be open while waiting for them.
            values = {
                portfolio.id: (await value_portfolio(session, self.market, portfolio)).total_value
                for portfolio in portfolios
                if portfolio.id not in done
            }
            for portfolio_id, value in values.items():
                session.add(
                    ValueSnapshot(portfolio_id=portfolio_id, at=now, value=value, kind="daily")
                )
                written += 1
            await session.commit()
        self._snapshot_day = now.date()
        return written

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
                if self._snapshot_day != self.market.now().date():
                    await self.daily_snapshots()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("matcher tick failed")
            await asyncio.sleep(TICK_SECONDS)

    def start(self) -> None:
        self._task = asyncio.create_task(self.run(), name="matcher")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
