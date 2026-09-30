import type { Asset, Quote } from '../api/types'
import { formatAge, formatDateTime } from '../lib/format'

/** The labels that say how far a price can be trusted. */
export default function QuoteLabels({ asset, quote }: { asset: Asset; quote: Quote | null | undefined }) {
  if (!asset.available) return <span className="badge unavailable">unavailable</span>
  if (!quote) return <span className="badge stale">no price</span>
  return (
    <>
      {!quote.market_open && (
        <span className="badge closed" title={`Opens ${formatDateTime(quote.next_open)}`}>
          closed
        </span>
      )}
      {quote.delayed && (
        <span className="badge delayed" title={`Price is ${formatAge(quote.age_seconds)} old`}>
          delayed
        </span>
      )}
      {quote.stale && quote.market_open && (
        <span className="badge stale" title={`Price is ${formatAge(quote.age_seconds)} old`}>
          stale
        </span>
      )}
      {quote.last_price_only && (
        <span className="badge" title="The source gives no bid and ask; fills assume a spread">
          last only
        </span>
      )}
    </>
  )
}
