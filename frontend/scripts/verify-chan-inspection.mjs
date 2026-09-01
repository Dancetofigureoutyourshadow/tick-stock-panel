import assert from 'node:assert/strict'

import { chartInspectionId } from '../src/lib/chan-inspection.ts'

assert.equal(chartInspectionId({ data: { inspectionId: 'fractal:3' } }), 'fractal:3')

assert.equal(
  chartInspectionId({ data: [{ inspectionId: 'stroke:2' }, { inspectionId: 'stroke:2' }] }),
  'stroke:2',
)

assert.equal(chartInspectionId({ data: { value: 12.3 } }), null)
assert.equal(chartInspectionId(null), null)

console.log('chan inspection event verification passed')
