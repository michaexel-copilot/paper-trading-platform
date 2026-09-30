import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Fragment, useState } from 'react'
import { Link, useParams } from 'react-router'
import { api, errorMessage } from '../api/client'
import {
  useFeeProfiles,
  useOrders,
  usePortfolio,
  usePortfolioFeeProfiles,
  useTrades,
  useValueHistory,
} from '../api/hooks'
import {
  ASSET_CLASSES,
  CLASS_LABELS,
  type FeeProfile,
  type PortfolioDetail,
  type Trade,
} from '../api/types'
import Chart from '../components/Chart'
import QuoteLabels from '../components/QuoteLabels'
import {
  formatAge,
  formatDate,
  formatDateTime,
  formatMoney,
  formatNumber,
  formatPercent,
  formatPrice,
  formatRate,
  formatSigned,
  signClass,
} from '../lib/format'

function Stat({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div>
      <span className="muted">{label}</span>
      <strong className={tone}>{value}</strong>
    </div>
  )
}

function Summary({ portfolio }: { portfolio: PortfolioDetail }) {
  const base = portfolio.base_currency
  return (
    <div className="stats" data-testid="summary">
      <Stat label="Total value" value={formatMoney(portfolio.total_value, base)} />
      <Stat
        label="Total return"
        value={`${formatSigned(portfolio.total_return, base)} (${formatPercent(portfolio.total_return_pct)})`}
        tone={signClass(portfolio.total_return)}
      />
      <Stat label="Cash" value={formatMoney(portfolio.cash, base)} />
      <Stat label="Available cash" value={formatMoney(portfolio.available_cash, base)} />
      <Stat
        label="Unrealised"
        value={formatSigned(portfolio.unrealized, base)}
        tone={signClass(portfolio.unrealized)}
      />
      <Stat
        label="Realised"
        value={formatSigned(portfolio.realized, base)}
        tone={signClass(portfolio.realized)}
      />
      <Stat label="Fees paid" value={formatMoney(portfolio.fees_paid, base)} />
      <Stat label="Starting cash" value={formatMoney(portfolio.starting_cash, base)} />
    </div>
  )
}

function ValueChart({ portfolioId }: { portfolioId: number }) {
  const { data: history } = useValueHistory(portfolioId)
  if (!history) return null
  return (
    <section className="card">
      <h2>Value over time</h2>
      <Chart
        kind="area"
        intraday
        data={history.map((point) => ({ time: point.at, value: point.value }))}
      />
      <p className="muted">
        {history.length} {history.length === 1 ? 'point' : 'points'} since{' '}
        {formatDate(history[0].at)}. One is recorded every day and after every fill.
      </p>
    </section>
  )
}

function Holdings({ portfolio }: { portfolio: PortfolioDetail }) {
  const base = portfolio.base_currency
  if (portfolio.positions.length === 0) {
    return <p className="muted">No positions yet. Pick an asset below or in the asset browser to trade.</p>
  }
  return (
    <table data-testid="holdings">
      <thead>
        <tr>
          <th>Asset</th>
          <th className="num">Quantity</th>
          <th className="num">Avg cost</th>
          <th className="num">Price</th>
          <th className="num">Market value</th>
          <th className="num">Unrealised</th>
          <th className="num">%</th>
          <th />
        </tr>
      </thead>
      <tbody>
        {portfolio.positions.map((position) => (
          <tr key={position.asset.id}>
            <td>
              <Link to={`/assets/${position.asset.id}?portfolio=${portfolio.id}`}>
                {position.asset.symbol}
              </Link>{' '}
              <span className="muted">{position.asset.name}</span>
            </td>
            <td className="num">
              {formatNumber(position.quantity)}
              {Number(position.committed) > 0 && (
                <span className="muted"> ({formatNumber(position.committed)} in sell orders)</span>
              )}
            </td>
            <td className="num">{`${formatNumber(position.avg_cost, 4, 2)} ${base}`}</td>
            <td className="num">{formatPrice(position.price, position.price_currency)}</td>
            <td className="num">{formatMoney(position.market_value, base)}</td>
            <td className={`num ${signClass(position.unrealized)}`}>
              {formatSigned(position.unrealized, base)}
            </td>
            <td className={`num ${signClass(position.unrealized)}`}>
              {formatPercent(position.unrealized_pct)}
            </td>
            <td>
              {position.unpriced && <span className="badge unavailable">unpriced</span>}
              {position.stale && !position.unpriced && <span className="badge stale">stale</span>}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function Tracked({ portfolio }: { portfolio: PortfolioDetail }) {
  const queryClient = useQueryClient()
  const remove = useMutation({
    mutationFn: (assetId: number) =>
      api(`/api/portfolios/${portfolio.id}/assets/${assetId}`, { method: 'DELETE' }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['portfolio', portfolio.id] }),
  })

  if (portfolio.tracked.length === 0) {
    return (
      <p className="muted">
        No assets in this portfolio yet. <Link to="/assets">Browse assets</Link> and add some.
      </p>
    )
  }
  return (
    <>
      {remove.error && (
        <p className="error" role="alert">
          {errorMessage(remove.error)}
        </p>
      )}
      <table data-testid="tracked">
        <thead>
          <tr>
            <th>Asset</th>
            <th>Class</th>
            <th className="num">Last</th>
            <th>Source</th>
            <th>Status</th>
            <th className="num">Position</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {portfolio.tracked.map(({ asset, quote, position_quantity }) => (
            <tr key={asset.id}>
              <td>
                <Link to={`/assets/${asset.id}?portfolio=${portfolio.id}`}>{asset.symbol}</Link>{' '}
                <span className="muted">{asset.name}</span>
              </td>
              <td>{CLASS_LABELS[asset.asset_class]}</td>
              <td className="num">{formatPrice(quote?.last, quote?.currency)}</td>
              <td>{quote?.source ?? asset.source ?? '–'}</td>
              <td>
                <QuoteLabels asset={asset} quote={quote} />
              </td>
              <td className="num">{formatNumber(position_quantity)}</td>
              <td className="actions">
                <Link to={`/assets/${asset.id}?portfolio=${portfolio.id}`}>Trade</Link>
                <button
                  className="link"
                  disabled={remove.isPending}
                  onClick={() => remove.mutate(asset.id)}
                >
                  Remove
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  )
}

function Orders({ portfolio }: { portfolio: PortfolioDetail }) {
  const queryClient = useQueryClient()
  const { data: orders } = useOrders(portfolio.id)
  const cancel = useMutation({
    mutationFn: (orderId: number) =>
      api(`/api/portfolios/${portfolio.id}/orders/${orderId}/cancel`, { method: 'POST' }),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ['portfolio', portfolio.id] }),
  })

  if (!orders) return <p className="muted">Loading…</p>
  if (orders.length === 0) return <p className="muted">No orders yet.</p>
  return (
    <>
      {cancel.error && (
        <p className="error" role="alert">
          {errorMessage(cancel.error)}
        </p>
      )}
      <table data-testid="orders">
        <thead>
          <tr>
            <th>Placed</th>
            <th>Asset</th>
            <th>Side</th>
            <th>Type</th>
            <th className="num">Quantity</th>
            <th className="num">Limit / stop</th>
            <th className="num">Reserved</th>
            <th>Status</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {orders.map((order) => (
            <tr key={order.id}>
              <td>{formatDateTime(order.created_at)}</td>
              <td>
                <Link to={`/assets/${order.asset_id}?portfolio=${portfolio.id}`}>
                  {order.asset_symbol}
                </Link>
              </td>
              <td className={order.side}>{order.side}</td>
              <td>{order.type}</td>
              <td className="num">{formatNumber(order.quantity)}</td>
              <td className="num">{formatPrice(order.limit_price ?? order.stop_price)}</td>
              <td className="num">
                {order.status === 'open' && order.side === 'buy'
                  ? formatMoney(order.reserved_cash, portfolio.base_currency)
                  : '–'}
              </td>
              <td>
                <span className={`badge ${order.status}`}>{order.status}</span>
                {order.reject_reason && <span className="muted"> {order.reject_reason}</span>}
              </td>
              <td className="actions">
                {order.status === 'open' && (
                  <button disabled={cancel.isPending} onClick={() => cancel.mutate(order.id)}>
                    Cancel
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  )
}

function TradeDetail({ trade, base }: { trade: Trade; base: string }) {
  const terms = trade.fee_terms as Record<string, string | boolean | null>
  const rate = trade.liquidity === 'maker' ? terms.maker_rate : terms.taker_rate
  return (
    <dl className="detail">
      <dt>Fill price</dt>
      <dd>{formatPrice(trade.price, trade.quote_currency)}</dd>
      <dt>Price source</dt>
      <dd>
        {trade.quote_source}, observed {formatDateTime(trade.quote_observed_at)}
      </dd>
      <dt>Conversion rate</dt>
      <dd>
        1 {trade.quote_currency} = {formatNumber(trade.fx_rate, 8)} {base}
      </dd>
      <dt>Value</dt>
      <dd>{formatMoney(trade.value_base, base)}</dd>
      <dt>Fee</dt>
      <dd>
        {formatMoney(trade.fee, base)} under “{trade.fee_profile_name}” ({trade.liquidity}
        {typeof rate === 'string' && Number(rate) > 0 && `, ${formatRate(rate)}`})
        {trade.fee_rate_fallback && ' · the pricing source publishes no rates, so the profile’s own were used'}
      </dd>
      <dt>Cash change</dt>
      <dd>{formatSigned(trade.cash_change, base)}</dd>
      {trade.side === 'sell' && (
        <>
          <dt>Realised</dt>
          <dd className={signClass(trade.realized_pnl)}>{formatSigned(trade.realized_pnl, base)}</dd>
        </>
      )}
    </dl>
  )
}

function Trades({ portfolio }: { portfolio: PortfolioDetail }) {
  const { data: trades } = useTrades(portfolio.id)
  const [open, setOpen] = useState<number | null>(null)
  const base = portfolio.base_currency

  if (!trades) return <p className="muted">Loading…</p>
  if (trades.length === 0) return <p className="muted">No trades yet.</p>
  return (
    <>
      <p>
        <a href={`/api/portfolios/${portfolio.id}/trades.csv`} download>
          Download trades as CSV
        </a>
      </p>
      <table data-testid="trades">
        <thead>
          <tr>
            <th>Time</th>
            <th>Asset</th>
            <th>Side</th>
            <th className="num">Quantity</th>
            <th className="num">Price</th>
            <th>Source</th>
            <th className="num">Fee</th>
            <th className="num">Cash change</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {[...trades].reverse().map((trade) => (
            <Fragment key={trade.id}>
              <tr>
                <td>{formatDateTime(trade.executed_at)}</td>
                <td>{trade.asset_symbol}</td>
                <td className={trade.side}>{trade.side}</td>
                <td className="num">{formatNumber(trade.quantity)}</td>
                <td className="num">{formatPrice(trade.price, trade.quote_currency)}</td>
                <td>{trade.quote_source}</td>
                <td className="num">{formatMoney(trade.fee, base)}</td>
                <td className={`num ${signClass(trade.cash_change)}`}>
                  {formatSigned(trade.cash_change, base)}
                </td>
                <td className="actions">
                  <button
                    className="link"
                    onClick={() => setOpen(open === trade.id ? null : trade.id)}
                  >
                    {open === trade.id ? 'Hide' : 'Details'}
                  </button>
                </td>
              </tr>
              {open === trade.id && (
                <tr className="expanded">
                  <td colSpan={9}>
                    <TradeDetail trade={trade} base={base} />
                  </td>
                </tr>
              )}
            </Fragment>
          ))}
        </tbody>
      </table>
    </>
  )
}

function charges(profile: FeeProfile): string[] {
  const list: string[] = []
  const currency = profile.currency ?? ''
  if (profile.exchange_rates) {
    for (const [exchange, rates] of Object.entries(profile.exchange_rates)) {
      list.push(`${exchange}: maker ${formatRate(rates.maker)}, taker ${formatRate(rates.taker)}`)
    }
    list.push(
      `other sources: maker ${formatRate(profile.maker_rate)}, taker ${formatRate(profile.taker_rate)}`,
    )
  } else if (Number(profile.maker_rate) > 0 || Number(profile.taker_rate) > 0) {
    list.push(
      profile.maker_rate === profile.taker_rate
        ? `${formatRate(profile.taker_rate)} of trade value`
        : `maker ${formatRate(profile.maker_rate)}, taker ${formatRate(profile.taker_rate)}`,
    )
  }
  if (Number(profile.per_unit) > 0) list.push(`${profile.per_unit} ${currency} per unit`)
  if (Number(profile.fixed) > 0) list.push(`${formatMoney(profile.fixed, currency)} per order`)
  if (profile.min_fee) list.push(`minimum ${formatMoney(profile.min_fee, currency)}`)
  if (profile.max_fee) list.push(`maximum ${formatMoney(profile.max_fee, currency)}`)
  if (profile.max_fee_rate) list.push(`maximum ${formatRate(profile.max_fee_rate)} of trade value`)
  if (Number(profile.assumed_spread) > 0) {
    list.push(`assumed spread ${formatRate(profile.assumed_spread)} when a quote has no bid and ask`)
  }
  return list.length > 0 ? list : ['no charges']
}

function Fees({ portfolio }: { portfolio: PortfolioDetail }) {
  const queryClient = useQueryClient()
  const { data: profiles } = useFeeProfiles()
  const { data: chosen } = usePortfolioFeeProfiles(portfolio.id)
  const base = portfolio.base_currency

  const choose = useMutation({
    mutationFn: ({ assetClass, key }: { assetClass: string; key: string }) =>
      api(`/api/portfolios/${portfolio.id}/fee-profiles/${assetClass}`, {
        method: 'PUT',
        body: { profile_key: key },
      }),
    onSuccess: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: ['portfolio', portfolio.id, 'fee-profiles'] }),
        queryClient.invalidateQueries({ queryKey: ['preview'] }),
      ]),
  })

  if (!profiles || !chosen) return <p className="muted">Loading…</p>
  return (
    <>
      <p className="muted">
        A change applies to fills from now on. Trades already made keep the fee they were charged.
      </p>
      {choose.error && (
        <p className="error" role="alert">
          {errorMessage(choose.error)}
        </p>
      )}
      <table data-testid="fees">
        <thead>
          <tr>
            <th>Asset class</th>
            <th>Fee profile</th>
            <th>Charges</th>
            <th>Source</th>
            <th className="num">Fees paid</th>
          </tr>
        </thead>
        <tbody>
          {ASSET_CLASSES.map((assetClass) => {
            const profile = chosen[assetClass]
            if (!profile) return null
            return (
              <tr key={assetClass}>
                <td>{CLASS_LABELS[assetClass]}</td>
                <td>
                  <select
                    aria-label={`Fee profile for ${CLASS_LABELS[assetClass]}`}
                    value={profile.key}
                    disabled={choose.isPending}
                    onChange={(e) => choose.mutate({ assetClass, key: e.target.value })}
                  >
                    {profiles
                      .filter((p) => p.asset_classes.includes(assetClass))
                      .map((p) => (
                        <option key={p.key} value={p.key}>
                          {p.name}
                        </option>
                      ))}
                  </select>
                  <div className="muted">{profile.venue}</div>
                </td>
                <td>
                  <ul className="plain">
                    {charges(profile).map((line) => (
                      <li key={line}>{line}</li>
                    ))}
                  </ul>
                </td>
                <td className="source">
                  {profile.source_note && <div className="muted">{profile.source_note}</div>}
                  {profile.source_url && (
                    <a href={profile.source_url} target="_blank" rel="noreferrer">
                      Published schedule
                    </a>
                  )}
                  <div className="muted">Checked {formatDate(profile.checked_on)}</div>
                </td>
                <td className="num">
                  {formatMoney(portfolio.fees_by_class[assetClass] ?? '0', base)}
                </td>
              </tr>
            )
          })}
        </tbody>
        <tfoot>
          <tr>
            <td colSpan={4}>Total</td>
            <td className="num">
              <strong>{formatMoney(portfolio.fees_paid, base)}</strong>
            </td>
          </tr>
        </tfoot>
      </table>
    </>
  )
}

const TABS = ['Orders', 'Trades', 'Fees'] as const

export default function PortfolioPage() {
  const portfolioId = Number(useParams().portfolioId)
  const [tab, setTab] = useState<(typeof TABS)[number]>('Orders')
  // Only this portfolio's assets are polled, faster when it tracks crypto.
  const { data: portfolio, isPending, error } = usePortfolio(portfolioId)

  if (isPending) return <p className="muted">Loading…</p>
  if (error) return <p className="error">{errorMessage(error)}</p>

  return (
    <>
      <p className="muted">
        <Link to="/portfolios">← Portfolios</Link>
      </p>
      <h1>
        {portfolio.name} <span className="muted">{portfolio.base_currency}</span>
      </h1>
      {portfolio.stale && (
        <p className="notice" role="status">
          Some positions are valued with stale prices, for example because their market is closed.
          The oldest price used is {formatAge(portfolio.oldest_quote_age_seconds)} old.
        </p>
      )}
      <Summary portfolio={portfolio} />
      <ValueChart portfolioId={portfolio.id} />

      <h2>Holdings</h2>
      <Holdings portfolio={portfolio} />

      <h2>Assets in this portfolio</h2>
      <Tracked portfolio={portfolio} />

      <div className="tabs" role="tablist">
        {TABS.map((name) => (
          <button
            key={name}
            role="tab"
            aria-selected={tab === name}
            className={tab === name ? 'active' : ''}
            onClick={() => setTab(name)}
          >
            {name}
          </button>
        ))}
      </div>
      {tab === 'Orders' && <Orders portfolio={portfolio} />}
      {tab === 'Trades' && <Trades portfolio={portfolio} />}
      {tab === 'Fees' && <Fees portfolio={portfolio} />}
    </>
  )
}
