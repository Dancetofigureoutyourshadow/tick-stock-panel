// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { RegimeConfigCard } from './RegimeConfigCard'
import type { DataStatus } from '@/lib/api'

vi.mock('@/lib/api', () => ({
  api: {
    preferences: async () => ({
      pipeline_regime_enabled: false,
      regime_batch_days: 60,
      regime_warmup_days: 40,
    }),
  },
}))

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
})

it('allows recompute when indicator partitions exist even though status rows is zero', async () => {
  const onRecompute = vi.fn()
  const render = async (enriched: DataStatus['enriched'], recomputeDisabled: boolean, recomputing = false) => {
    await act(async () => root.render(
      <QueryClientProvider client={client}>
        <RegimeConfigCard
          onRecompute={onRecompute}
          recomputing={recomputing}
          recomputeDisabled={recomputeDisabled}
          enriched={enriched}
          recomputeError={null}
          recomputeResult={null}
        />
      </QueryClientProvider>,
    ))
  }
  const button = () => [...host.querySelectorAll('button')].find(el => el.textContent?.includes('全量重算') || el.textContent?.includes('重算中'))!

  await render(null, false)
  expect(button().disabled).toBe(true)
  expect(host.textContent).toContain('请先生成股票指标数据')

  const enriched = {
    rows: 0,
    earliest_date: '2004-03-08',
    latest_date: '2026-10-15',
    symbols_covered: 5000,
    trading_days: 5493,
  }
  await render(enriched, false)
  expect(button().disabled).toBe(false)
  await act(async () => button().click())
  expect(onRecompute).toHaveBeenCalledTimes(1)

  await render(enriched, true)
  expect(button().disabled).toBe(true)
  expect(host.textContent).toContain('请等待当前数据任务完成')

  await render(enriched, false, true)
  expect(button().disabled).toBe(true)
  expect(button().textContent).toContain('重算中')
})
