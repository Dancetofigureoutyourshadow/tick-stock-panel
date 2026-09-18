import assert from 'node:assert/strict'

import { chartInspectionId } from '../src/lib/chart-inspection.ts'

assert.equal(chartInspectionId({ data: { inspectionId: 'marker:3' } }), 'marker:3')

assert.equal(
  chartInspectionId({ data: [{ inspectionId: 'range:2' }, { inspectionId: 'range:2' }] }),
  'range:2',
)

assert.equal(chartInspectionId({ data: { value: 12.3 } }), null)
assert.equal(chartInspectionId(null), null)

console.log('chart inspection event verification passed')
