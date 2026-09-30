import { describe, expect, it } from 'vitest'
import { isAtLeast, isMultipleOf, parseDecimal, validateOrder, type OrderDraft } from './order'

const stock = { quantity_step: '1', min_order_size: null }
const bitcoin = { quantity_step: '0.00000001', min_order_size: '0.00001' }

function draft(overrides: Partial<OrderDraft>): OrderDraft {
  return { type: 'market', quantity: '1', limitPrice: '', stopPrice: '', ...overrides }
}

describe('parseDecimal', () => {
  it('keeps every digit of a long fraction', () => {
    expect(parseDecimal('0.123456789012345678')).toEqual({ digits: 123456789012345678n, scale: 18 })
  })

  it('rejects anything that is not a plain decimal', () => {
    for (const text of ['', 'abc', '-1', '1e5', '1,5', '.5', '1.']) {
      expect(parseDecimal(text)).toBeNull()
    }
  })
})

describe('isMultipleOf', () => {
  it('accepts whole shares and refuses fractions of a share', () => {
    expect(isMultipleOf('3', '1')).toBe(true)
    expect(isMultipleOf('1.5', '1')).toBe(false)
  })

  it('is exact where floating point is not', () => {
    // 0.3 % 0.1 is 0.09999999999999998 in floating point.
    expect(isMultipleOf('0.3', '0.1')).toBe(true)
    expect(isMultipleOf('0.00000003', '0.00000001')).toBe(true)
    expect(isMultipleOf('0.000000031', '0.00000001')).toBe(false)
  })
})

describe('isAtLeast', () => {
  it('compares across different scales', () => {
    expect(isAtLeast('0.00001', '0.00001')).toBe(true)
    expect(isAtLeast('0.000009', '0.00001')).toBe(false)
    expect(isAtLeast('10', '9.999')).toBe(true)
  })
})

describe('validateOrder', () => {
  it('accepts a valid market order', () => {
    expect(validateOrder(draft({ quantity: '5' }), stock)).toBeNull()
  })

  it('asks for a quantity', () => {
    expect(validateOrder(draft({ quantity: '' }), stock)).toBe('Enter a quantity.')
  })

  it('refuses zero and non-numeric quantities', () => {
    expect(validateOrder(draft({ quantity: '0' }), stock)).toMatch(/greater than zero/)
    expect(validateOrder(draft({ quantity: 'ten' }), stock)).toMatch(/greater than zero/)
  })

  it('names the permitted step', () => {
    expect(validateOrder(draft({ quantity: '1.5' }), stock)).toBe(
      'Quantity must be a multiple of 1.',
    )
  })

  it('names the minimum order size', () => {
    expect(validateOrder(draft({ quantity: '0.000001' }), bitcoin)).toBe(
      'The minimum order size is 0.00001.',
    )
    expect(validateOrder(draft({ quantity: '0.00001' }), bitcoin)).toBeNull()
  })

  it('requires a limit price for a limit order', () => {
    expect(validateOrder(draft({ type: 'limit' }), stock)).toMatch(/limit price/)
    expect(validateOrder(draft({ type: 'limit', limitPrice: '0' }), stock)).toMatch(/limit price/)
    expect(validateOrder(draft({ type: 'limit', limitPrice: '99.5' }), stock)).toBeNull()
  })

  it('requires a stop price for a stop order', () => {
    expect(validateOrder(draft({ type: 'stop' }), stock)).toMatch(/stop price/)
    expect(validateOrder(draft({ type: 'stop', stopPrice: '90' }), stock)).toBeNull()
  })

  it('ignores prices that do not belong to the order type', () => {
    expect(validateOrder(draft({ type: 'market', limitPrice: 'x', stopPrice: 'y' }), stock)).toBeNull()
  })
})
