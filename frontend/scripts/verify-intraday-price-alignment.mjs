import assert from 'node:assert/strict'

import {
  alignMinutePricesToDailyClose,
  computeIntradayAverage,
} from '../src/lib/intraday-chart.ts'

const rows = [
  { datetime: '2020-01-02 09:30:00', open: 19.20, high: 19.30, low: 19.10, close: 19.25, volume: 100, amount: 192_500 },
  { datetime: '2020-01-02 15:00:00', open: 19.48, high: 19.55, low: 19.45, close: 19.52, volume: 200, amount: 390_400 },
]

const aligned = alignMinutePricesToDailyClose(rows, 14.95)

assert.ok(Math.abs(aligned.priceScale - 14.95 / 19.52) < 1e-12)
assert.ok(Math.abs(aligned.rows.at(-1).close - 14.95) < 1e-12)
assert.equal(aligned.rows[0].amount, rows[0].amount)
assert.ok(Math.max(...aligned.rows.map(row => row.high)) < 16)

const averages = computeIntradayAverage(aligned.rows, aligned.priceScale)
assert.ok(averages.every(value => value > 14 && value < 16))

console.log('intraday price-alignment verification passed')
