import { useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router'
import { errorMessage } from '../api/client'
import { pollInterval, useAsset, useHistory, useQuotes } from '../api/hooks'
import { CLASS_LABELS, type Asset } from '../api/types'
import Chart from '../components/Chart'
import OrderTicket from '../components/OrderTicket'
import QuoteLabels from '../components/QuoteLabels'
import { formatAge, formatDateTime, formatPrice } from '../lib/format'

const RANGES = {
  '1d': { label: 'Daily, 1 year', days: 365 },
  '1h': { label: 'Hourly, 30 days', days: 30 },
} as const

type Resolution = keyof typeof RANGES

function rangeFor(resolution: Resolution): { resolution: Resolution; start: string } {
  const day = new Date()
  day.setUTCHours(0, 0, 0, 0)
  day.setUTCDate(day.getUTCDate() - RANGES[resolution].days)
  return { resolution, start: day.toISOString() }
}

/** What a trader has to know about how this asset class is simulated. */
function notices(asset: Asset): string[] {
  const list: string[] = []
  if (asset.asset_class !== 'crypto') {
    list.push(
      'Prices come from Yahoo Finance and can be delayed by up to 15 minutes. Orders fill at the delayed price, and each trade records when its price was observed.',
    )
  }
  if (asset.asset_class === 'commodities') {
    list.push(
      'This is the front-month futures price. When the contract rolls to the next month the price can jump, which shows up as profit or loss that a holder of the real contract would not have had. A position is unleveraged exposure to the quoted price, without contract size, margin or expiry.',
    )
  }
  if (asset.asset_class === 'stocks' || asset.asset_class === 'etfs') {
    list.push(
      'Stock splits and dividends are not applied to positions. After a split the price drops while the quantity held stays the same.',
    )
  }
  return list
}

function PriceChart({ asset }: { asset: Asset }) {
  // The start is fixed when a range is chosen, so the request stays stable while the page is open.
  const [{ resolution, start }, setRange] = useState(() => rangeFor('1d'))
  const setResolution = (next: Resolution) => setRange(rangeFor(next))
  const { data, isPending, error } = useHistory(asset.id, resolution, start)

  return (
    <section className="card">
      <div className="row between">
        <h2>Price</h2>
        <div className="tabs small">
          {(Object.keys(RANGES) as Resolution[]).map((key) => (
            <button
              key={key}
              className={resolution === key ? 'active' : ''}
              onClick={() => setResolution(key)}
            >
              {RANGES[key].label}
            </button>
          ))}
        </div>
      </div>
      {isPending && <p className="muted">Loading price history…</p>}
      {error && <p className="error">{errorMessage(error)}</p>}
      {data && data.bars.length === 0 && <p className="muted">No price history for this range.</p>}
      {data && data.bars.length > 0 && (
        <>
          <Chart kind="candles" data={data.bars} intraday={resolution === '1h'} />
          <p className="muted">
            {data.bars.length} bars from {data.source} in {asset.quote_currency}.
          </p>
        </>
      )}
    </section>
  )
}

export default function AssetPage() {
  const assetId = Number(useParams().assetId)
  const [params] = useSearchParams()
  const portfolioParam = params.get('portfolio')
  const { data: asset, isPending, error } = useAsset(assetId)
  const { data: quotes } = useQuotes(
    asset?.available ? [assetId] : [],
    pollInterval(asset ? [asset.asset_class] : []),
  )

  if (isPending) return <p className="muted">Loading…</p>
  if (error) return <p className="error">{errorMessage(error)}</p>

  const quote = quotes?.[assetId]

  return (
    <>
      <p className="muted">
        <Link to={`/assets?class=${asset.asset_class}`}>← {CLASS_LABELS[asset.asset_class]}</Link>
      </p>
      <h1>
        {asset.name} <span className="muted">{asset.symbol}</span>
      </h1>
      <div className="stats">
        <div>
          <span className="muted">Last</span>
          <strong>{formatPrice(quote?.last, quote?.currency ?? asset.quote_currency)}</strong>
        </div>
        <div>
          <span className="muted">Bid / Ask</span>
          <strong>
            {formatPrice(quote?.bid)} / {formatPrice(quote?.ask)}
          </strong>
        </div>
        <div>
          <span className="muted">Market</span>
          <strong data-testid="market-status">
            {!quote ? '–' : quote.market_open ? 'Open' : 'Closed'}
          </strong>
          {quote && !quote.market_open && quote.next_open && (
            <span className="muted">opens {formatDateTime(quote.next_open)}</span>
          )}
        </div>
        <div>
          <span className="muted">Source</span>
          <strong>{quote?.source ?? asset.source ?? '–'}</strong>
          {quote && <span className="muted">observed {formatAge(quote.age_seconds)} ago</span>}
        </div>
        <div>
          <span className="muted">Status</span>
          <span>
            <QuoteLabels asset={asset} quote={quotes ? quote : undefined} />
            {quote?.fresh && !quote.delayed && <span className="badge fresh">live</span>}
          </span>
        </div>
      </div>

      {notices(asset).map((text) => (
        <p key={text} className="notice">
          {text}
        </p>
      ))}

      <div className="split">
        <PriceChart asset={asset} />
        <OrderTicket
          asset={asset}
          initialPortfolioId={portfolioParam ? Number(portfolioParam) : undefined}
        />
      </div>
    </>
  )
}
