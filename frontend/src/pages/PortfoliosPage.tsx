import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState, type FormEvent } from 'react'
import { Link } from 'react-router'
import { api, errorMessage } from '../api/client'
import { usePortfolios } from '../api/hooks'
import type { PortfolioSummary } from '../api/types'
import { formatMoney, formatPercent, formatSigned, signClass } from '../lib/format'

function CreateForm() {
  const queryClient = useQueryClient()
  const [name, setName] = useState('')
  const [currency, setCurrency] = useState('EUR')
  const [cash, setCash] = useState('10000')

  const create = useMutation({
    mutationFn: () =>
      api<PortfolioSummary>('/api/portfolios', {
        method: 'POST',
        body: { name, base_currency: currency, starting_cash: cash },
      }),
    onSuccess: () => {
      setName('')
      return queryClient.invalidateQueries({ queryKey: ['portfolios'] })
    },
  })

  function onSubmit(event: FormEvent) {
    event.preventDefault()
    create.mutate()
  }

  return (
    <form className="card row" onSubmit={onSubmit}>
      <label>
        Name
        <input name="name" required value={name} onChange={(e) => setName(e.target.value)} />
      </label>
      <label>
        Base currency
        <select name="currency" value={currency} onChange={(e) => setCurrency(e.target.value)}>
          <option>EUR</option>
          <option>USD</option>
        </select>
      </label>
      <label>
        Starting cash
        <input
          name="cash"
          inputMode="decimal"
          required
          value={cash}
          onChange={(e) => setCash(e.target.value)}
        />
      </label>
      <button type="submit" className="primary" disabled={create.isPending}>
        Create portfolio
      </button>
      {create.error && (
        <p className="error wide" role="alert">
          {errorMessage(create.error)}
        </p>
      )}
      <p className="muted wide">Base currency and starting cash cannot be changed later.</p>
    </form>
  )
}

function Row({ portfolio }: { portfolio: PortfolioSummary }) {
  const queryClient = useQueryClient()
  const [mode, setMode] = useState<'view' | 'rename' | 'delete'>('view')
  const [name, setName] = useState(portfolio.name)
  const refresh = () => queryClient.invalidateQueries({ queryKey: ['portfolios'] })

  const rename = useMutation({
    mutationFn: () =>
      api(`/api/portfolios/${portfolio.id}`, { method: 'PATCH', body: { name } }),
    onSuccess: () => {
      setMode('view')
      return refresh()
    },
  })
  const remove = useMutation({
    mutationFn: () => api(`/api/portfolios/${portfolio.id}?confirm=true`, { method: 'DELETE' }),
    onSuccess: refresh,
  })

  return (
    <>
      <tr>
        <td>
          {mode === 'rename' ? (
            <form
              className="inline"
              onSubmit={(event) => {
                event.preventDefault()
                rename.mutate()
              }}
            >
              <input
                aria-label="New name"
                value={name}
                autoFocus
                onChange={(e) => setName(e.target.value)}
              />
              <button type="submit" disabled={rename.isPending}>
                Save
              </button>
              <button type="button" className="link" onClick={() => setMode('view')}>
                Cancel
              </button>
            </form>
          ) : (
            <Link to={`/portfolios/${portfolio.id}`}>{portfolio.name}</Link>
          )}
        </td>
        <td>{portfolio.base_currency}</td>
        <td className="num">
          {formatMoney(portfolio.total_value, portfolio.base_currency)}
          {portfolio.stale && <span className="badge stale">stale prices</span>}
        </td>
        <td className={`num ${signClass(portfolio.total_return)}`}>
          {formatSigned(portfolio.total_return, portfolio.base_currency)}
        </td>
        <td className={`num ${signClass(portfolio.total_return)}`}>
          {formatPercent(portfolio.total_return_pct)}
        </td>
        <td className="actions">
          <button className="link" onClick={() => setMode('rename')}>
            Rename
          </button>
          <button className="link danger" onClick={() => setMode('delete')}>
            Delete
          </button>
        </td>
      </tr>
      {mode === 'delete' && (
        <tr className="confirm">
          <td colSpan={6}>
            Delete <strong>{portfolio.name}</strong> with all its positions, orders, trades and
            history? This cannot be undone.{' '}
            <button className="danger" disabled={remove.isPending} onClick={() => remove.mutate()}>
              Delete permanently
            </button>{' '}
            <button className="link" onClick={() => setMode('view')}>
              Keep it
            </button>
          </td>
        </tr>
      )}
      {(rename.error || remove.error) && (
        <tr>
          <td colSpan={6} className="error" role="alert">
            {errorMessage(rename.error ?? remove.error)}
          </td>
        </tr>
      )}
    </>
  )
}

export default function PortfoliosPage() {
  const { data: portfolios, isPending, error } = usePortfolios()

  return (
    <>
      <h1>Portfolios</h1>
      <CreateForm />
      {isPending && <p className="muted">Loading…</p>}
      {error && <p className="error">{errorMessage(error)}</p>}
      {portfolios && portfolios.length === 0 && (
        <p className="muted">No portfolios yet. Create one to start trading.</p>
      )}
      {portfolios && portfolios.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Name</th>
              <th>Currency</th>
              <th className="num">Total value</th>
              <th className="num">Return</th>
              <th className="num">Return %</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {portfolios.map((portfolio) => (
              <Row key={portfolio.id} portfolio={portfolio} />
            ))}
          </tbody>
        </table>
      )}
    </>
  )
}
