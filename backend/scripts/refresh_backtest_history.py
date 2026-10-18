"""CLI: fill/update the on-disk OKX OHLCV cache for backtests (HED-119).

Only public OKX endpoints (ccxt), no key. Safe to run repeatedly -- each
call only fetches bars newer than what is already cached.

    uv run python scripts/refresh_backtest_history.py BTC/USDC ETH/USDC SOL/USDC
    uv run python scripts/refresh_backtest_history.py --timeframe 1h BTC/USDC
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.backtest_data.historical import DEFAULT_UNIVERSE, HistoryCache, refresh  # noqa: E402
from app.config import get_settings  # noqa: E402


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbols", nargs="*", default=list(DEFAULT_UNIVERSE))
    parser.add_argument("--timeframe", default="1d", choices=("1d", "1h"))
    parser.add_argument("--cache-dir", default=None)
    args = parser.parse_args()

    cache_dir = Path(args.cache_dir or get_settings().backtest_cache_dir)
    cache = HistoryCache(cache_dir)

    for symbol in args.symbols:
        result = await refresh(cache, symbol, args.timeframe)
        print(
            f"{symbol} {args.timeframe}: {result.bar_count} Balken, "
            f"Beginn {result.listing_start}, {len(result.gaps)} Lücke(n), "
            f"{len(result.implausible_bars)} verworfene Platzhalter-Kerze(n)"
        )
        for gap in result.gaps:
            print(f"  Lücke: {gap.after} .. {gap.before} ({gap.missing_bars} fehlend)")
        for implausible in result.implausible_bars:
            print(f"  Verworfen ({implausible.time}): {implausible.reason}")


if __name__ == "__main__":
    asyncio.run(main())
