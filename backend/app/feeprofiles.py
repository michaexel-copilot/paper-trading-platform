"""Fee profiles: seeding the built-in ones and resolving the terms for a fill."""

from datetime import date
from decimal import Decimal
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import FeeProfile, PortfolioFeeProfile
from app.trading.fees import FeeTerms

SEED_FILE = Path(__file__).parent / "seed" / "fee_profiles.yaml"

_DECIMAL_FIELDS = ("maker_rate", "taker_rate", "per_unit", "fixed", "assumed_spread")
_OPTIONAL_DECIMAL_FIELDS = ("min_fee", "max_fee", "max_fee_rate")


def load_seed(path: Path = SEED_FILE) -> list[dict]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["profiles"]


def _to_date(value) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


async def seed_fee_profiles(session: AsyncSession, entries: list[dict] | None = None) -> None:
    """Create or update the built-in profiles, keyed on ``key``."""
    entries = load_seed() if entries is None else entries
    existing = {p.key: p for p in (await session.scalars(select(FeeProfile))).all()}
    for entry in entries:
        profile = existing.get(entry["key"])
        if profile is None:
            profile = FeeProfile(key=entry["key"])
            session.add(profile)
        profile.name = entry["name"]
        profile.venue = entry["venue"]
        profile.asset_classes = list(entry["asset_classes"])
        profile.default_for = list(entry.get("default_for", []))
        profile.currency = entry.get("currency")
        for field in _DECIMAL_FIELDS:
            setattr(profile, field, Decimal(str(entry.get(field, "0"))))
        for field in _OPTIONAL_DECIMAL_FIELDS:
            value = entry.get(field)
            setattr(profile, field, Decimal(str(value)) if value is not None else None)
        profile.exchange_rates = entry.get("exchange_rates")
        profile.source_url = entry.get("source_url")
        profile.source_note = entry.get("source_note")
        profile.checked_on = _to_date(entry["checked_on"])
    await session.commit()


async def default_profiles(session: AsyncSession) -> dict[str, FeeProfile]:
    """The default profile of each asset class."""
    defaults: dict[str, FeeProfile] = {}
    for profile in (await session.scalars(select(FeeProfile).order_by(FeeProfile.id))).all():
        for asset_class in profile.default_for:
            defaults.setdefault(asset_class, profile)
    return defaults


async def profile_for(session: AsyncSession, portfolio_id: int, asset_class: str) -> FeeProfile:
    profile = await session.scalar(
        select(FeeProfile)
        .join(PortfolioFeeProfile, PortfolioFeeProfile.fee_profile_id == FeeProfile.id)
        .where(
            PortfolioFeeProfile.portfolio_id == portfolio_id,
            PortfolioFeeProfile.asset_class == asset_class,
        )
    )
    if profile is None:
        profile = (await default_profiles(session))[asset_class]
    return profile


def terms_for(profile: FeeProfile, pricing_source: str | None) -> FeeTerms:
    """The profile's terms for a fill priced by ``pricing_source``.

    A profile with per-exchange rates charges the rates of the exchange that
    priced the fill, and falls back to its own rates for any other source.
    """
    terms = FeeTerms.from_profile(profile)
    if not profile.exchange_rates:
        return terms
    published = profile.exchange_rates.get(pricing_source or "")
    if not published:
        return terms.with_exchange_rates(None, None)
    return terms.with_exchange_rates(Decimal(published["maker"]), Decimal(published["taker"]))
