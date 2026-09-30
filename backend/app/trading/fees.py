"""Fee and fill-price rules. Pure functions, no I/O."""

from dataclasses import asdict, dataclass, replace
from decimal import ROUND_HALF_UP, Decimal

from app.marketdata.base import Quote

CENT = Decimal("0.01")
ZERO = Decimal(0)


def round_money(amount: Decimal) -> Decimal:
    return amount.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class FeeTerms:
    """The charges of a fee profile as they apply to one fill."""

    profile_key: str
    profile_name: str
    currency: str | None = None  # currency of the absolute charges
    maker_rate: Decimal = ZERO
    taker_rate: Decimal = ZERO
    per_unit: Decimal = ZERO
    fixed: Decimal = ZERO
    min_fee: Decimal | None = None
    max_fee: Decimal | None = None
    max_fee_rate: Decimal | None = None
    assumed_spread: Decimal = ZERO
    # True when exchange-published rates were wanted and the profile's own were used.
    rate_fallback: bool = False

    @classmethod
    def from_profile(cls, profile) -> "FeeTerms":
        return cls(
            profile_key=profile.key,
            profile_name=profile.name,
            currency=profile.currency,
            maker_rate=profile.maker_rate,
            taker_rate=profile.taker_rate,
            per_unit=profile.per_unit,
            fixed=profile.fixed,
            min_fee=profile.min_fee,
            max_fee=profile.max_fee,
            max_fee_rate=profile.max_fee_rate,
            assumed_spread=profile.assumed_spread,
        )

    def with_exchange_rates(self, maker: Decimal | None, taker: Decimal | None) -> "FeeTerms":
        """Use the rates the pricing exchange publishes, or flag the fallback."""
        if maker is None or taker is None:
            return replace(self, rate_fallback=True)
        return replace(self, maker_rate=maker, taker_rate=taker, rate_fallback=False)

    def rate(self, liquidity: str) -> Decimal:
        return self.maker_rate if liquidity == "maker" else self.taker_rate

    def as_record(self) -> dict:
        return {
            k: (format(v, "f") if isinstance(v, Decimal) else v) for k, v in asdict(self).items()
        }


def fee_in_fee_currency(
    terms: FeeTerms, *, value: Decimal, quantity: Decimal, liquidity: str
) -> Decimal:
    """The unrounded fee, with ``value`` already expressed in the profile's currency."""
    fee = terms.rate(liquidity) * value + terms.per_unit * quantity + terms.fixed
    if terms.min_fee is not None:
        fee = max(fee, terms.min_fee)
    if terms.max_fee is not None:
        fee = min(fee, terms.max_fee)
    if terms.max_fee_rate is not None:
        fee = min(fee, terms.max_fee_rate * value)
    return max(fee, ZERO)


def compute_fee(
    terms: FeeTerms,
    *,
    value: Decimal,
    quantity: Decimal,
    liquidity: str,
    quote_to_fee_currency: Decimal = Decimal(1),
    fee_currency_to_base: Decimal = Decimal(1),
) -> Decimal:
    """Fee of a fill in the portfolio's base currency, rounded half-up to cents.

    ``value`` is the trade value in the asset's quote currency. The fee is worked
    out in the profile's currency and converted to the base currency last.
    """
    fee = fee_in_fee_currency(
        terms, value=value * quote_to_fee_currency, quantity=quantity, liquidity=liquidity
    )
    return round_money(fee * fee_currency_to_base)


def fill_price(quote: Quote, side: str, assumed_spread: Decimal = ZERO) -> Decimal:
    """Price a price-taking order gets: the ask for a buy, the bid for a sell.

    A quote without bid and ask is moved against the trader by half the assumed spread.
    """
    if not quote.last_price_only:
        return quote.ask if side == "buy" else quote.bid
    half = assumed_spread / 2
    return quote.last * (1 + half) if side == "buy" else quote.last * (1 - half)
