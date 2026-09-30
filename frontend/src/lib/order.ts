/**
 * Client-side checks for the order ticket. Amounts arrive from the API as
 * decimal strings and are compared as scaled integers, never as floats.
 */

export type OrderType = 'market' | 'limit' | 'stop'

export interface OrderDraft {
  type: OrderType
  quantity: string
  limitPrice: string
  stopPrice: string
}

export interface AssetRules {
  quantity_step: string
  min_order_size: string | null
}

const DECIMAL = /^\d+(\.\d+)?$/

/** Parse a plain, non-negative decimal string into an integer and its scale. */
export function parseDecimal(text: string): { digits: bigint; scale: number } | null {
  const trimmed = text.trim()
  if (!DECIMAL.test(trimmed)) return null
  const [whole, fraction = ''] = trimmed.split('.')
  return { digits: BigInt(whole + fraction), scale: fraction.length }
}

function align(a: { digits: bigint; scale: number }, b: { digits: bigint; scale: number }) {
  const scale = Math.max(a.scale, b.scale)
  return [
    a.digits * 10n ** BigInt(scale - a.scale),
    b.digits * 10n ** BigInt(scale - b.scale),
  ] as const
}

export function isPositive(text: string): boolean {
  const value = parseDecimal(text)
  return value !== null && value.digits > 0n
}

export function isMultipleOf(quantity: string, step: string): boolean {
  const q = parseDecimal(quantity)
  const s = parseDecimal(step)
  if (!q || !s || s.digits === 0n) return false
  const [left, right] = align(q, s)
  return left % right === 0n
}

export function isAtLeast(quantity: string, minimum: string): boolean {
  const q = parseDecimal(quantity)
  const m = parseDecimal(minimum)
  if (!q || !m) return false
  const [left, right] = align(q, m)
  return left >= right
}

/** The first problem with the draft, or null when it can be sent. */
export function validateOrder(draft: OrderDraft, asset: AssetRules): string | null {
  if (draft.quantity.trim() === '') return 'Enter a quantity.'
  if (!isPositive(draft.quantity)) return 'Quantity must be a number greater than zero.'
  if (!isMultipleOf(draft.quantity, asset.quantity_step)) {
    return `Quantity must be a multiple of ${asset.quantity_step}.`
  }
  if (asset.min_order_size !== null && !isAtLeast(draft.quantity, asset.min_order_size)) {
    return `The minimum order size is ${asset.min_order_size}.`
  }
  if (draft.type === 'limit' && !isPositive(draft.limitPrice)) {
    return 'A limit order needs a limit price greater than zero.'
  }
  if (draft.type === 'stop' && !isPositive(draft.stopPrice)) {
    return 'A stop order needs a stop price greater than zero.'
  }
  return null
}
