import assert from 'node:assert/strict'

import { formatPriceAxisLabel } from '../src/lib/chart-axis.ts'

assert.equal(formatPriceAxisLabel(91.57894737), '91.58')
assert.equal(formatPriceAxisLabel(20), '20')
assert.equal(formatPriceAxisLabel(0.12345), '0.123')
assert.equal(formatPriceAxisLabel(Number.NaN), 'NaN')

console.log('candlestick price-axis formatter verification passed')
