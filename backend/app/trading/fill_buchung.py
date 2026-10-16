"""Shared Position/Cash booking for one fill (HED-109, Entwurf Abschnitt 4.1).

Extracted from ``engine.try_fill`` so the manual dashboard matcher and the proposal
pipeline (``stop_pflicht._buche_einstiegsfill``) cannot drift apart -- same reasoning
as the ``engine.py`` module docstring, now for two callers instead of one. No branch
here is new; this is a pure extraction of the existing booking logic.
"""

from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Portfolio, Position
from app.trading.fees import ZERO, round_money

if TYPE_CHECKING:
    from app.trading.engine import Amounts


async def buche_fill(
    session: AsyncSession,
    portfolio: Portfolio,
    asset_id: int,
    side: str,
    price: Decimal,
    quantity: Decimal,
    fx_rate: Decimal,
    result: "Amounts",
) -> Decimal:
    """Book one fill's Position/avg_cost and Portfolio cash/fees/realized_pnl.

    ``portfolio`` must already be loaded (and, for a caller that needs the usual
    concurrency safety, locked with ``with_for_update()``) by the caller -- this
    function does not load or lock it itself, same as the original code it was
    extracted from. Returns the realized P&L of this fill (zero for a buy).
    """
    position = await session.scalar(
        select(Position).where(Position.portfolio_id == portfolio.id, Position.asset_id == asset_id)
    )
    held = position.quantity if position else ZERO

    realized = ZERO
    if side == "buy":
        # Average cost uses the unrounded trade value and excludes the fee.
        cost = price * quantity * fx_rate
        if position is None:
            position = Position(
                portfolio_id=portfolio.id, asset_id=asset_id, quantity=ZERO, avg_cost=ZERO
            )
            session.add(position)
        total = held + quantity
        position.avg_cost = (held * position.avg_cost + cost) / total
        position.quantity = total
    else:
        realized = result.value_base - round_money(position.avg_cost * quantity)
        position.quantity = held - quantity
        if position.quantity == 0:
            await session.delete(position)

    portfolio.cash += result.cash_change
    portfolio.fees_paid += result.fee
    portfolio.realized_pnl += realized
    return realized
