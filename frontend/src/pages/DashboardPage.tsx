import { Fragment, useState } from 'react'
import { errorMessage } from '../api/client'
import {
  useApproveProposal,
  useChangeProposal,
  useDashboardQueue,
  useDashboardStatus,
  useDiscardProposal,
} from '../api/hooks'
import type { Proposal } from '../api/types'
import { formatAge, formatDateTime, formatNumber, formatPrice, formatRate } from '../lib/format'

/** Feld 15 (Entwurf 5.1): live Zustand der zwei Sperrkennzeichen. Kein Platzhalter --
 * ein Lesefehler würde serverseitig schon fail-closed als 'gesetzt' zurückkommen. */
function LockBadges({ proposal }: { proposal: Proposal }) {
  const { firmenweit_gesperrt, strategie_gesperrt } = proposal.lock_state
  if (!firmenweit_gesperrt && !strategie_gesperrt) {
    return <span className="badge filled">offen</span>
  }
  return (
    <>
      {firmenweit_gesperrt && <span className="badge rejected">firmenweit gesperrt</span>}
      {strategie_gesperrt && <span className="badge rejected">Strategie gesperrt</span>}
    </>
  )
}

const PROPOSAL_KIND_LABEL: Record<string, string> = {
  entry: 'Einstieg',
  increase: 'Erhöhung',
}

/** Feld 14 (Entwurf 5.1): macht eine geänderte Fassung als solche erkennbar. */
function ChangeHistory({ proposal }: { proposal: Proposal }) {
  if (proposal.change_generation === 0 && proposal.parent_proposal_id === null) {
    return <span>Erstfassung</span>
  }
  return (
    <span>
      Änderung Nr. {proposal.change_generation}
      {proposal.parent_proposal_id !== null && ` (Vorgänger: #${proposal.parent_proposal_id})`}
    </span>
  )
}

function Countdown({ seconds }: { seconds: number | null }) {
  if (seconds === null) return <span className="muted">–</span>
  if (seconds <= 0) return <span className="loss">verfallen</span>
  const urgent = seconds < 900
  return <span className={urgent ? 'loss' : undefined}>{formatAge(seconds)}</span>
}

function Stage1Table({ proposal }: { proposal: Proposal }) {
  const rows = proposal.limit_utilization as
    | { name?: string; value?: string; limit?: string; utilization?: string }[]
    | null
  return (
    <dl className="detail">
      <dt>Stufe-1-Ergebnis</dt>
      <dd>
        <span className={`badge ${proposal.stage1_result === 'passed' ? 'filled' : 'rejected'}`}>
          {proposal.stage1_result ?? 'unbekannt'}
        </span>
      </dd>
      <dt>NAV</dt>
      <dd>{formatNumber(proposal.nav, 2, 2)}</dd>
      <dt>Drawdown</dt>
      <dd>{proposal.drawdown ? formatRate(proposal.drawdown) : '–'}</dd>
      <dt>Summenrisiko vor Trade</dt>
      <dd>
        {proposal.aggregate_risk_utilization_before
          ? formatRate(proposal.aggregate_risk_utilization_before)
          : '–'}
      </dd>
      <dt>Summenrisiko nach Trade</dt>
      <dd>
        {proposal.aggregate_risk_utilization_after
          ? formatRate(proposal.aggregate_risk_utilization_after)
          : '–'}
      </dd>
      <dt>Risiko je Trade</dt>
      <dd>{proposal.risk_eur ? `${formatNumber(proposal.risk_eur, 2, 2)} EUR` : '–'}</dd>
      <dt>Korrelation zu offenen Positionen</dt>
      <dd>
        {proposal.correlation_to_open_positions ?? '–'}
        <p className="muted">
          Liste der Positionen mit dem höchsten Beitrag: noch nicht verfügbar. Offene Abhängigkeit
          zu HED-44 (Stufe-1-Berechnung liefert bisher nur die Kennzahl, keine Aufschlüsselung je
          Position).
        </p>
      </dd>
      <dt>Kursalter</dt>
      <dd>{formatAge(proposal.price_age_seconds ?? null)}</dd>
      <dt>Strategie</dt>
      <dd>Strategie #{proposal.strategy_id}</dd>
      <dt>Vorschlagsart</dt>
      <dd>{PROPOSAL_KIND_LABEL[proposal.proposal_kind] ?? proposal.proposal_kind}</dd>
      <dt>Stop-Abstand</dt>
      <dd>
        {formatNumber(proposal.stop_distance, 8, 2)} ({formatRate(proposal.stop_distance_pct)})
      </dd>
      <dt>Preisband-Breite / Grenzpreis</dt>
      <dd>
        {proposal.limit_price !== null ? (
          formatPrice(proposal.limit_price)
        ) : (
          <span className="muted">noch nicht verfügbar, folgt mit HED-44</span>
        )}
      </dd>
      <dt>Änderungshistorie</dt>
      <dd>
        <ChangeHistory proposal={proposal} />
      </dd>
      {rows && rows.length > 0 && (
        <>
          <dt>Limittabelle</dt>
          <dd>
            <table>
              <thead>
                <tr>
                  <th>Kennzahl</th>
                  <th className="num">Wert</th>
                  <th className="num">Grenze</th>
                  <th className="num">Auslastung</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row, i) => {
                  const value = Number(row.value)
                  const limit = Number(row.limit)
                  const derivedUtilization =
                    row.utilization ??
                    (row.value !== undefined &&
                    row.limit !== undefined &&
                    Number.isFinite(value) &&
                    Number.isFinite(limit) &&
                    limit !== 0
                      ? `${((value / limit) * 100).toFixed(2)} %`
                      : undefined)
                  return (
                    <tr key={row.name ?? i}>
                      <td>{row.name ?? '–'}</td>
                      <td className="num">{row.value ?? '–'}</td>
                      <td className="num">{row.limit ?? '–'}</td>
                      <td className="num">{derivedUtilization ?? '–'}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </dd>
        </>
      )}
    </dl>
  )
}

function ChangeForm({
  proposal,
  onCancel,
}: {
  proposal: Proposal
  onCancel: () => void
}) {
  const change = useChangeProposal()
  const [size, setSize] = useState(proposal.size)
  const [proposalPrice, setProposalPrice] = useState(proposal.proposal_price)
  const [stopPrice, setStopPrice] = useState(proposal.stop_price)
  const [reason, setReason] = useState('')

  return (
    <form
      className="stack"
      onSubmit={(e) => {
        e.preventDefault()
        change.mutate(
          {
            proposalId: proposal.id,
            body: {
              reason,
              size: size !== proposal.size ? size : undefined,
              proposal_price: proposalPrice !== proposal.proposal_price ? proposalPrice : undefined,
              stop_price: stopPrice !== proposal.stop_price ? stopPrice : undefined,
            },
          },
          { onSuccess: onCancel },
        )
      }}
    >
      {change.error && (
        <p className="error" role="alert">
          {errorMessage(change.error)}
        </p>
      )}
      <label>
        Größe
        <input value={size} onChange={(e) => setSize(e.target.value)} />
      </label>
      <label>
        Vorschlagskurs
        <input value={proposalPrice} onChange={(e) => setProposalPrice(e.target.value)} />
      </label>
      <label>
        Stop-Kurs
        <input value={stopPrice} onChange={(e) => setStopPrice(e.target.value)} />
      </label>
      <label>
        Begründung (Pflicht)
        <input value={reason} onChange={(e) => setReason(e.target.value)} required minLength={1} />
      </label>
      <div className="actions">
        <button type="submit" disabled={change.isPending}>
          Geänderten Vorschlag anlegen
        </button>
        <button type="button" className="link" onClick={onCancel}>
          Abbrechen
        </button>
      </div>
      <p className="muted">
        Der alte Vorschlag endet damit; der geänderte läuft als neuer Vorschlag vollständig erneut
        durch Stufe 1 (Entwurf 5.2).
      </p>
    </form>
  )
}

function ProposalRow({ proposal }: { proposal: Proposal }) {
  const [open, setOpen] = useState(false)
  const [changing, setChanging] = useState(false)
  const approve = useApproveProposal()
  const discard = useDiscardProposal()

  function onApprove() {
    if (!window.confirm(`Vorschlag ${proposal.id} (${proposal.instrument}) wirklich freigeben?`)) {
      return
    }
    approve.mutate({ proposalId: proposal.id, body: { confirm: true } })
  }

  function onDiscard() {
    const reason = window.prompt('Begründung für das Verwerfen (optional):') ?? undefined
    discard.mutate({ proposalId: proposal.id, body: { reason } })
  }

  const error = approve.error ?? discard.error

  return (
    <Fragment>
      <tr>
        <td>{formatDateTime(proposal.signal_at)}</td>
        <td>
          {proposal.instrument} <LockBadges proposal={proposal} />
        </td>
        <td className={proposal.direction}>{proposal.direction}</td>
        <td className="num">{formatNumber(proposal.size)}</td>
        <td className="num">{formatPrice(proposal.proposal_price)}</td>
        <td className="num">{formatPrice(proposal.stop_price)}</td>
        <td className="num">{proposal.risk_eur ? `${formatNumber(proposal.risk_eur, 2, 2)} EUR` : '–'}</td>
        <td className="num">
          {proposal.aggregate_risk_utilization_after
            ? formatRate(proposal.aggregate_risk_utilization_after)
            : '–'}
        </td>
        <td>
          <Countdown seconds={proposal.remaining_seconds} />
        </td>
        <td className="actions">
          <button
            className="link"
            onClick={() => {
              setOpen(!open)
              setChanging(false)
            }}
          >
            {open ? 'Weniger' : 'Details'}
          </button>
          <button disabled={approve.isPending} onClick={onApprove}>
            Freigeben
          </button>
          <button className="link" onClick={() => setChanging(!changing)}>
            Ändern
          </button>
          <button className="link" disabled={discard.isPending} onClick={onDiscard}>
            Verwerfen
          </button>
        </td>
      </tr>
      {error && (
        <tr>
          <td colSpan={10}>
            <p className="error" role="alert">
              {errorMessage(error)}
            </p>
          </td>
        </tr>
      )}
      {changing && (
        <tr className="expanded">
          <td colSpan={10}>
            <ChangeForm proposal={proposal} onCancel={() => setChanging(false)} />
          </td>
        </tr>
      )}
      {open && (
        <tr className="expanded">
          <td colSpan={10}>
            <Stage1Table proposal={proposal} />
          </td>
        </tr>
      )}
    </Fragment>
  )
}

export default function DashboardPage() {
  const { data: proposals, isPending, error } = useDashboardQueue()
  const { data: status } = useDashboardStatus()

  if (isPending) return <p className="muted">Loading…</p>
  if (error) return <p className="error">{errorMessage(error)}</p>

  return (
    <>
      <h1>Handelsvorschläge</h1>
      <p className="muted">
        Jeder Vorschlag wartet auf eine Einzelentscheidung. Ohne Freigabe wird nicht gehandelt.
      </p>
      {status && status.notifications_failing > 0 && (
        <p className="notice" role="status">
          Die Board-Benachrichtigung per E-Mail ist für {status.notifications_failing}{' '}
          {status.notifications_failing === 1 ? 'Vorschlag' : 'Vorschläge'} dauerhaft fehlgeschlagen.
          Der CRO wurde einzeln benachrichtigt; die Entscheidung hier im Dashboard ist davon
          unabhängig weiter möglich.
        </p>
      )}
      {proposals.length === 0 ? (
        <p className="muted">Keine Vorschläge warten derzeit auf eine Entscheidung.</p>
      ) : (
        <table data-testid="dashboard-queue">
          <thead>
            <tr>
              <th>Signal</th>
              <th>Instrument</th>
              <th>Richtung</th>
              <th className="num">Größe</th>
              <th className="num">Kurs</th>
              <th className="num">Stop</th>
              <th className="num">Risiko</th>
              <th className="num">Summenrisiko nach Trade</th>
              <th>Restzeit</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {proposals.map((proposal) => (
              <ProposalRow key={proposal.id} proposal={proposal} />
            ))}
          </tbody>
        </table>
      )}
    </>
  )
}
