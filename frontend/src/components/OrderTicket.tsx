import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState, type FormEvent } from 'react'
import { Link } from 'react-router'
import { api, ApiError, errorMessage } from '../api/client'
import { CRYPTO_POLL_MS, usePortfolio, usePortfolios } from '../api/hooks'
import type { Asset, Order, Preview, PreviewIn } from '../api/types'
import { formatDateTime, formatMoney, formatNumber, formatPrice } from '../lib/format'
import { validateOrder, type OrderType } from '../lib/order'

function newOrderId(): string {
  return crypto.randomUUID()
}

function useDebounced<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delayMs)
    return () => clearTimeout(timer)
  }, [value, delayMs])
  return debounced
}

function refusalText(error: unknown): string {
  if (error instanceof ApiError && error.code === 'market_closed' && error.detail.next_open) {
    return `${error.message} It opens ${formatDateTime(String(error.detail.next_open))}.`
  }
  return errorMessage(error)
}

function outcomeText(order: Order): string {
  if (order.status === 'filled') return `Order filled. See the trade in the portfolio.`
  if (order.status === 'open') return 'Order accepted. It stays open until its price is reached.'
  return `Order ${order.status}${order.reject_reason ? `: ${order.reject_reason}` : ''}.`
}

export default function OrderTicket({ asset, initialPortfolioId }: { asset: Asset; initialPortfolioId?: number }) {
  const queryClient = useQueryClient()
  const { data: portfolios } = usePortfolios()
  const [portfolioId, setPortfolioId] = useState<number | undefined>(initialPortfolioId)
  const [side, setSide] = useState<'buy' | 'sell'>('buy')
  const [type, setType] = useState<OrderType>('market')
  const [quantity, setQuantity] = useState('')
  const [limitPrice, setLimitPrice] = useState('')
  const [stopPrice, setStopPrice] = useState('')
  // One id per ticket: a double click or a retried request cannot create a second order.
  const [clientOrderId, setClientOrderId] = useState(newOrderId)

  const selected = portfolioId ?? portfolios?.[0]?.id
  const portfolio = portfolios?.find((p) => p.id === selected)
  const { data: detail } = usePortfolio(selected, CRYPTO_POLL_MS * 2)
  const held = detail?.positions.find((p) => p.asset.id === asset.id)

  const problem = validateOrder({ type, quantity, limitPrice, stopPrice }, asset)
  const request: PreviewIn = {
    asset_id: asset.id,
    side,
    type,
    quantity,
    limit_price: type === 'limit' ? limitPrice : null,
    stop_price: type === 'stop' ? stopPrice : null,
  }
  const previewKey = useDebounced(JSON.stringify(request), 300)
  const preview = useQuery({
    queryKey: ['preview', selected, previewKey],
    queryFn: () =>
      api<Preview>(`/api/portfolios/${selected}/orders/preview`, {
        method: 'POST',
        body: JSON.parse(previewKey),
      }),
    enabled: selected !== undefined && problem === null && previewKey === JSON.stringify(request),
    retry: false,
    refetchInterval: asset.asset_class === 'crypto' ? CRYPTO_POLL_MS : 30_000,
  })

  const place = useMutation({
    mutationFn: () =>
      api<Order>(`/api/portfolios/${selected}/orders`, {
        method: 'POST',
        body: { ...request, client_order_id: clientOrderId },
      }),
    onSuccess: () => {
      setClientOrderId(newOrderId())
      setQuantity('')
      return Promise.all([
        queryClient.invalidateQueries({ queryKey: ['portfolio', selected] }),
        queryClient.invalidateQueries({ queryKey: ['portfolios'] }),
      ])
    },
  })

  function onSubmit(event: FormEvent) {
    event.preventDefault()
    if (problem === null) place.mutate()
  }

  function edit<T>(setter: (value: T) => void) {
    return (value: T) => {
      place.reset()
      setter(value)
    }
  }

  if (!asset.available) {
    return (
      <section className="card">
        <h2>Trade</h2>
        <p className="error">This asset is unavailable: no source can price it, so it cannot be traded.</p>
      </section>
    )
  }
  if (portfolios && portfolios.length === 0) {
    return (
      <section className="card">
        <h2>Trade</h2>
        <p>
          <Link to="/portfolios">Create a portfolio</Link> to trade this asset.
        </p>
      </section>
    )
  }

  const base = portfolio?.base_currency ?? ''
  const estimate = preview.data

  return (
    <section className="card ticket">
      <h2>Trade {asset.symbol}</h2>
      <form className="stack" onSubmit={onSubmit}>
        <label>
          Portfolio
          <select
            name="portfolio"
            value={selected ?? ''}
            onChange={(e) => edit(setPortfolioId)(Number(e.target.value))}
          >
            {portfolios?.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name} ({p.base_currency})
              </option>
            ))}
          </select>
        </label>
        {detail && (
          <p className="muted">
            Available cash {formatMoney(detail.available_cash, detail.base_currency)} · Holding{' '}
            {formatNumber(held?.quantity ?? '0')} {asset.symbol}
            {held && Number(held.committed) > 0 && ` (${formatNumber(held.committed)} in open sell orders)`}
          </p>
        )}
        <div className="segmented">
          {(['buy', 'sell'] as const).map((value) => (
            <button
              key={value}
              type="button"
              className={`${side === value ? 'active' : ''} ${value}`}
              onClick={() => edit(setSide)(value)}
            >
              {value === 'buy' ? 'Buy' : 'Sell'}
            </button>
          ))}
        </div>
        <label>
          Order type
          <select name="type" value={type} onChange={(e) => edit(setType)(e.target.value as OrderType)}>
            <option value="market">Market: fill now at the current price</option>
            <option value="limit">Limit: fill at this price or better</option>
            <option value="stop">Stop: fill once the price reaches this level</option>
          </select>
        </label>
        <label>
          Quantity
          <input
            name="quantity"
            inputMode="decimal"
            autoComplete="off"
            placeholder={`Step ${asset.quantity_step}${asset.min_order_size ? `, minimum ${asset.min_order_size}` : ''}`}
            value={quantity}
            onChange={(e) => edit(setQuantity)(e.target.value)}
          />
        </label>
        {type === 'limit' && (
          <label>
            Limit price ({asset.quote_currency})
            <input
              name="limit_price"
              inputMode="decimal"
              autoComplete="off"
              value={limitPrice}
              onChange={(e) => edit(setLimitPrice)(e.target.value)}
            />
          </label>
        )}
        {type === 'stop' && (
          <label>
            Stop price ({asset.quote_currency})
            <input
              name="stop_price"
              inputMode="decimal"
              autoComplete="off"
              value={stopPrice}
              onChange={(e) => edit(setStopPrice)(e.target.value)}
            />
          </label>
        )}

        {quantity !== '' && problem && <p className="error">{problem}</p>}
        {problem === null && preview.error && (
          <p className="error" role="alert">
            {refusalText(preview.error)}
          </p>
        )}
        {problem === null && estimate && (
          <dl className="preview" data-testid="preview">
            <dt>Estimated price</dt>
            <dd>{formatPrice(estimate.estimated_price, estimate.price_currency)}</dd>
            <dt>Value</dt>
            <dd>{formatMoney(estimate.value_base, base)}</dd>
            <dt>Fee ({estimate.fee_profile_name})</dt>
            <dd>{formatMoney(estimate.fee, base)}</dd>
            <dt>{side === 'buy' ? 'Total cost' : 'Net proceeds'}</dt>
            <dd>
              <strong>{formatMoney(estimate.cash_change.replace('-', ''), base)}</strong>
            </dd>
            <dt>Execution</dt>
            <dd>
              {estimate.fills_now
                ? 'Fills immediately'
                : `Rests until the price is reached (${estimate.liquidity} fee)`}
            </dd>
          </dl>
        )}

        {place.error && (
          <p className="error" role="alert">
            {refusalText(place.error)}
          </p>
        )}
        {place.data && (
          <p className="notice" role="status">
            {outcomeText(place.data)}{' '}
            <Link to={`/portfolios/${place.data.portfolio_id}`}>Open portfolio</Link>
          </p>
        )}
        <button
          type="submit"
          className={`primary ${side}`}
          disabled={problem !== null || place.isPending || selected === undefined}
        >
          {side === 'buy' ? 'Buy' : 'Sell'} {asset.symbol}
        </button>
      </form>
    </section>
  )
}
