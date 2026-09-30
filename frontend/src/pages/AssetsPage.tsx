import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState, type FormEvent } from 'react'
import { Link, useSearchParams } from 'react-router'
import { api, errorMessage } from '../api/client'
import { pollInterval, useAssets, usePortfolios, useQuotes, useSearch } from '../api/hooks'
import { ASSET_CLASSES, CLASS_LABELS, type Asset, type SearchResult } from '../api/types'
import QuoteLabels from '../components/QuoteLabels'
import { formatPrice } from '../lib/format'

function AddToPortfolio({ asset }: { asset: Asset }) {
  const queryClient = useQueryClient()
  const { data: portfolios } = usePortfolios()
  const [added, setAdded] = useState<string | null>(null)

  const add = useMutation({
    mutationFn: (portfolioId: number) =>
      api(`/api/portfolios/${portfolioId}/assets`, {
        method: 'POST',
        body: { asset_id: asset.id },
      }),
    onSuccess: (_, portfolioId) => {
      setAdded(portfolios?.find((p) => p.id === portfolioId)?.name ?? 'portfolio')
      return queryClient.invalidateQueries({ queryKey: ['portfolio', portfolioId] })
    },
  })

  if (!asset.available) return null
  if (!portfolios || portfolios.length === 0) {
    return <Link to="/portfolios">Create a portfolio</Link>
  }
  return (
    <>
      <select
        aria-label={`Add ${asset.symbol} to a portfolio`}
        value=""
        disabled={add.isPending}
        onChange={(e) => e.target.value && add.mutate(Number(e.target.value))}
      >
        <option value="">Add to portfolio…</option>
        {portfolios.map((portfolio) => (
          <option key={portfolio.id} value={portfolio.id}>
            {portfolio.name}
          </option>
        ))}
      </select>
      {added && !add.error && <span className="muted"> Added to {added}</span>}
      {add.error && <span className="error"> {errorMessage(add.error)}</span>}
    </>
  )
}

function AssetTable({ assetClass }: { assetClass: string }) {
  const { data: assets, isPending, error } = useAssets(assetClass)
  const { data: quotes } = useQuotes(
    (assets ?? []).filter((a) => a.available).map((a) => a.id),
    pollInterval([assetClass]),
  )

  if (isPending) return <p className="muted">Loading…</p>
  if (error) return <p className="error">{errorMessage(error)}</p>

  return (
    <table>
      <thead>
        <tr>
          <th>#</th>
          <th>Symbol</th>
          <th>Name</th>
          <th className="num">Last</th>
          <th className="num">Bid</th>
          <th className="num">Ask</th>
          <th>Source</th>
          <th>Status</th>
          <th />
        </tr>
      </thead>
      <tbody>
        {assets.map((asset) => {
          const quote = quotes?.[asset.id]
          return (
            <tr key={asset.id} className={asset.available ? '' : 'dim'}>
              <td className="muted">{asset.rank ?? ''}</td>
              <td>
                <Link to={`/assets/${asset.id}`}>{asset.symbol}</Link>
              </td>
              <td>{asset.name}</td>
              <td className="num">{formatPrice(quote?.last, quote?.currency)}</td>
              <td className="num">{formatPrice(quote?.bid)}</td>
              <td className="num">{formatPrice(quote?.ask)}</td>
              <td>{quote?.source ?? asset.source ?? '–'}</td>
              <td>
                <QuoteLabels asset={asset} quote={quotes ? quote : undefined} />
              </td>
              <td className="actions">
                <AddToPortfolio asset={asset} />
              </td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}

function SearchPanel() {
  const queryClient = useQueryClient()
  const [text, setText] = useState('')
  const [query, setQuery] = useState('')
  const { data: results, isFetching, error } = useSearch(query)

  const add = useMutation({
    mutationFn: (result: SearchResult) =>
      api<Asset>('/api/assets', {
        method: 'POST',
        body: { source: result.source, symbol: result.symbol },
      }),
    onSuccess: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: ['assets'] }),
        queryClient.invalidateQueries({ queryKey: ['search'] }),
      ]),
  })

  function onSubmit(event: FormEvent) {
    event.preventDefault()
    add.reset()
    setQuery(text.trim())
  }

  return (
    <section className="card">
      <form className="row" onSubmit={onSubmit}>
        <label className="grow">
          Find an asset beyond the top ten
          <input
            name="search"
            placeholder="Symbol or name, for example SAP or PEPE"
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
        </label>
        <button type="submit" disabled={isFetching}>
          Search
        </button>
      </form>
      {error && <p className="error">{errorMessage(error)}</p>}
      {add.error && (
        <p className="error" role="alert">
          {errorMessage(add.error)}
        </p>
      )}
      {results && results.length === 0 && <p className="muted">Nothing found for “{query}”.</p>}
      {results && results.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Symbol</th>
              <th>Name</th>
              <th>Class</th>
              <th>Source</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {results.map((result) => (
              <tr key={`${result.source}:${result.symbol}`}>
                <td>{result.symbol}</td>
                <td>{result.name}</td>
                <td>
                  {result.asset_class
                    ? CLASS_LABELS[result.asset_class]
                    : `${result.instrument_type} (not supported)`}
                </td>
                <td>{result.source}</td>
                <td className="actions">
                  {result.asset_id ? (
                    <Link to={`/assets/${result.asset_id}`}>In catalog</Link>
                  ) : result.supported ? (
                    <button disabled={add.isPending} onClick={() => add.mutate(result)}>
                      Add to catalog
                    </button>
                  ) : (
                    <span className="muted">Instrument type not supported</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  )
}

export default function AssetsPage() {
  const [params, setParams] = useSearchParams()
  const requested = params.get('class') ?? ''
  const assetClass = (ASSET_CLASSES as readonly string[]).includes(requested) ? requested : 'crypto'

  return (
    <>
      <h1>Assets</h1>
      <div className="tabs" role="tablist">
        {ASSET_CLASSES.map((name) => (
          <button
            key={name}
            role="tab"
            aria-selected={name === assetClass}
            className={name === assetClass ? 'active' : ''}
            onClick={() => setParams({ class: name })}
          >
            {CLASS_LABELS[name]}
          </button>
        ))}
      </div>
      <AssetTable assetClass={assetClass} />
      <SearchPanel />
    </>
  )
}
