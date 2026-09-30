/** Display formatting only. Numbers are never computed here, just shown. */

const LOCALE = 'en-GB'

export function formatNumber(value: string | null | undefined, maxDigits = 8, minDigits = 0): string {
  if (value === null || value === undefined) return '–'
  const number = Number(value)
  if (!Number.isFinite(number)) return value
  return number.toLocaleString(LOCALE, {
    minimumFractionDigits: minDigits,
    maximumFractionDigits: Math.max(maxDigits, minDigits),
  })
}

/** Cash amounts: two decimals and the currency code. */
export function formatMoney(value: string | null | undefined, currency: string): string {
  if (value === null || value === undefined) return '–'
  return `${formatNumber(value, 2, 2)} ${currency}`
}

/** Prices keep the precision the source gives them, with at least two decimals. */
export function formatPrice(value: string | null | undefined, currency?: string): string {
  if (value === null || value === undefined) return '–'
  const text = formatNumber(value, 8, 2)
  return currency ? `${text} ${currency}` : text
}

export function formatPercent(value: string | null | undefined): string {
  if (value === null || value === undefined) return '–'
  const number = Number(value)
  const sign = number > 0 ? '+' : ''
  return `${sign}${formatNumber(value, 2, 2)} %`
}

export function formatSigned(value: string, currency: string): string {
  const sign = Number(value) > 0 ? '+' : ''
  return `${sign}${formatMoney(value, currency)}`
}

export function signClass(value: string | null | undefined): string {
  const number = Number(value ?? 0)
  if (number > 0) return 'gain'
  if (number < 0) return 'loss'
  return ''
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return '–'
  return new Date(iso).toLocaleString(LOCALE, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

export function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString(LOCALE, { year: 'numeric', month: 'short', day: 'numeric' })
}

export function formatAge(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '–'
  if (seconds < 90) return `${Math.round(seconds)} s`
  if (seconds < 90 * 60) return `${Math.round(seconds / 60)} min`
  if (seconds < 36 * 3600) return `${Math.round(seconds / 3600)} h`
  return `${Math.round(seconds / 86400)} days`
}

/** A rate such as 0.001 shown as "0.10 %". */
export function formatRate(value: string): string {
  return `${formatNumber(String(Number(value) * 100), 4, 2)} %`
}
