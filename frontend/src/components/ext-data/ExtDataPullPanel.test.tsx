// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api, type ExtDataConfig } from '@/lib/api'
import { ExtDataPullPanel } from './ExtDataPullPanel'

vi.mock('@/lib/api', () => ({ api: {
  extDataPullConfig: vi.fn(), extDataApiKey: vi.fn(), extDataApiKeySet: vi.fn(),
} }))
vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })

const config: ExtDataConfig = {
  id: 'auction', label: '竞价', mode: 'timeseries', fields: [],
  created_at: '2026-10-19', updated_at: '2026-10-19',
  pull: {
    url: 'https://example.test/snapshots', method: 'GET', headers: {},
    response_path: 'data', field_map: { code: 'symbol' },
    schedule_minutes: 60, enabled: false, schedule_time: '09:30',
    schedule_period: 'weekly', schedule_day: 3, schedule_trading_only: true,
    auth: { type: 'bearer', key_ref: 'existing-credential' },
    date_param: 'date', date_format: 'compact', time_field: 'ts',
    timeout_seconds: 120, page_param: 'page', page_start: 0,
    page_size_param: 'size', page_size: 100, max_pages: 40,
    fanout: {
      url: 'https://example.test/members', param: 'id', id_field: 'id',
      member_id_field: 'code', symbol_col: 'symbol', name_col: 'name', value_col: 'tags',
    },
  },
}

let cleanup = async () => {}
beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api.extDataPullConfig).mockResolvedValue({ status: 'ok', pull: config.pull! })
  vi.mocked(api.extDataApiKey).mockResolvedValue({ key_set: true, masked_key: '***' })
})
afterEach(async () => { await cleanup() })

function render() {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const root = createRoot(host)
  cleanup = async () => { await act(async () => root.unmount()); host.remove() }
  act(() => root.render(<ExtDataPullPanel config={config} onSaved={vi.fn()} />))
  return host
}

async function clickButton(host: HTMLElement, text: string) {
  const button = [...host.querySelectorAll('button')].find(b => b.textContent?.trim() === text)
  expect(button).toBeDefined()
  await act(async () => button!.click())
}

it('切换启用状态保留调度、分页、鉴权及两级拉取配置', async () => {
  const host = render()
  await act(async () => (host.querySelector('[role="switch"]') as HTMLButtonElement).click())
  expect(api.extDataPullConfig).toHaveBeenCalledWith('auction', expect.objectContaining({
    ...config.pull, enabled: true,
  }))
  expect(api.extDataApiKeySet).not.toHaveBeenCalled()
})

it('设置弹窗保存同时保留旧配置及新增周期设置', async () => {
  const host = render()
  await clickButton(host, '拉取设置…')
  expect(host.textContent).toContain('历史回补')
  expect(host.textContent).toContain('每周三')
  await clickButton(host, '保存配置')
  expect(api.extDataPullConfig).toHaveBeenCalledWith('auction', expect.objectContaining({
    schedule_time: '09:30', schedule_period: 'weekly', schedule_day: 3,
    schedule_trading_only: true, auth: expect.objectContaining({ key_ref: 'existing-credential' }),
    page_start: 0, fanout: config.pull!.fanout, date_format: 'compact', time_field: 'ts',
  }))
  expect(api.extDataApiKeySet).not.toHaveBeenCalled()
})

it('切回间隔调度清除固定周期设置且保留数据解析配置', async () => {
  const host = render()
  await clickButton(host, '拉取设置…')
  await clickButton(host, '按间隔')
  await clickButton(host, '保存配置')
  expect(api.extDataPullConfig).toHaveBeenCalledWith('auction', expect.objectContaining({
    schedule_time: null, schedule_period: null, schedule_day: null,
    schedule_trading_only: false, schedule_minutes: 60,
    page_start: 0, fanout: config.pull!.fanout, time_field: 'ts',
  }))
})
