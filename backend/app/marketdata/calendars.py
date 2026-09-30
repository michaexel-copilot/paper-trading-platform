"""Trading hours per asset.

An asset's calendar code is one of:

* ``24/7`` — always open (crypto)
* ``FOREX`` — Sunday 22:00 UTC to Friday 22:00 UTC
* an ``exchange-calendars`` code such as ``XNYS``, ``XETR`` or ``CMES``
* ``WD|<timezone>|<HH:MM>|<HH:MM>`` — weekdays between two local times, without
  holidays, for exchanges that have no calendar in ``exchange-calendars``
"""

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

ALWAYS_OPEN = "24/7"
FOREX = "FOREX"
FUTURES = "CMES"

FOREX_OPEN = time(22, 0)

# Yahoo exchange codes to exchange-calendars codes.
CALENDAR_BY_YAHOO_EXCHANGE = {
    "NMS": "XNAS",
    "NGM": "XNAS",
    "NCM": "XNAS",
    "NYQ": "XNYS",
    "PCX": "XNYS",  # NYSE Arca
    "ASE": "XNYS",  # NYSE American
    "BTS": "XNYS",  # Cboe BZX keeps NYSE hours and holidays
    "GER": "XETR",
    "FRA": "XFRA",
    "LSE": "XLON",
    "PAR": "XPAR",
    "AMS": "XAMS",
    "BRU": "XBRU",
    "MIL": "XMIL",
    "MCE": "XMAD",
    "EBS": "XSWX",
    "VIE": "XWBO",
    "STO": "XSTO",
    "CPH": "XCSE",
    "HEL": "XHEL",
    "OSL": "XOSL",
    "TOR": "XTSE",
    "JPX": "XTKS",
    "HKG": "XHKG",
    "ASX": "XASX",
    # US futures exchanges share the CME Globex session calendar.
    "CMX": FUTURES,
    "NYM": FUTURES,
    "CBT": FUTURES,
    "CME": FUTURES,
    "NYB": FUTURES,
    "CCY": FOREX,
    "CCC": ALWAYS_OPEN,
}


@dataclass(frozen=True, slots=True)
class MarketStatus:
    is_open: bool
    next_open: datetime | None  # None while open


@lru_cache(maxsize=64)
def _exchange_calendar(code: str):
    import exchange_calendars

    return exchange_calendars.get_calendar(code)


def is_known_calendar(code: str) -> bool:
    if code in (ALWAYS_OPEN, FOREX) or code.startswith("WD|"):
        return True
    import exchange_calendars

    return code in exchange_calendars.get_calendar_names()


def _forex_status(now: datetime) -> MarketStatus:
    weekday, clock = now.weekday(), now.time()
    is_open = (
        weekday in (0, 1, 2, 3)
        or (weekday == 4 and clock < FOREX_OPEN)
        or (weekday == 6 and clock >= FOREX_OPEN)
    )
    if is_open:
        return MarketStatus(True, None)
    days_to_sunday = (6 - weekday) % 7
    sunday = (now + timedelta(days=days_to_sunday)).date()
    return MarketStatus(False, datetime.combine(sunday, FOREX_OPEN, tzinfo=UTC))


def _weekday_status(code: str, now: datetime) -> MarketStatus:
    _, zone_name, opens_text, closes_text = code.split("|")
    opens, closes = time.fromisoformat(opens_text), time.fromisoformat(closes_text)
    zone = ZoneInfo(zone_name)
    local = now.astimezone(zone)
    if local.weekday() < 5 and opens <= local.time() < closes:
        return MarketStatus(True, None)
    day = local.date()
    if local.weekday() >= 5 or local.time() >= closes:
        day += timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return MarketStatus(False, datetime.combine(day, opens, tzinfo=zone).astimezone(UTC))


def _exchange_status(code: str, now: datetime) -> MarketStatus:
    import pandas as pd

    calendar = _exchange_calendar(code)
    minute = pd.Timestamp(now).tz_convert("UTC").floor("min")
    if calendar.is_open_on_minute(minute):
        return MarketStatus(True, None)
    return MarketStatus(False, calendar.next_open(minute).to_pydatetime().astimezone(UTC))


def market_status(code: str, now: datetime) -> MarketStatus:
    """Whether the market with this calendar is open at ``now`` (timezone-aware)."""
    now = now.astimezone(UTC)
    if code == ALWAYS_OPEN:
        return MarketStatus(True, None)
    if code == FOREX:
        return _forex_status(now)
    if code.startswith("WD|"):
        return _weekday_status(code, now)
    return _exchange_status(code, now)
