import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const preview = readFileSync(new URL('../src/components/StockPreviewDialog.tsx', import.meta.url), 'utf8')
const multiDay = readFileSync(new URL('../src/components/StockMultiDayIntradayChart.tsx', import.meta.url), 'utf8')
const stockPanel = readFileSync(new URL('../src/components/StockPanel.tsx', import.meta.url), 'utf8')

const dailyBranchStart = preview.indexOf("{view === 'daily' ? (")
const dailyPanelStart = preview.indexOf('<StockPanel', dailyBranchStart)
const dailyPanelEnd = preview.indexOf('/>', dailyPanelStart)
assert.ok(dailyBranchStart >= 0 && dailyPanelStart >= 0 && dailyPanelEnd >= 0, '日 K 渲染分支不存在')
assert.match(
  preview,
  /w-\[96vw\] max-w-\[1440px\]/,
  '个股弹窗必须为三列盘口布局保留足够宽度',
)
assert.match(
  preview.slice(dailyPanelStart, dailyPanelEnd),
  /\bshowDepth5\b/,
  '日 K 视图必须启用五档和分时成交面板',
)

assert.match(
  multiDay,
  /<StockTransactionsPanel\s[\s\S]*?rows=\{focusStream\.transactions\}/,
  '多日分时视图必须渲染实时成交面板',
)
assert.doesNotMatch(
  multiDay,
  /focusStream\.marketPhase !== 'closed'/,
  '多日分时成交面板必须在收盘后保留最后数据',
)
assert.doesNotMatch(
  stockPanel,
  /focusStream\.marketPhase !== 'closed'/,
  '日 K 成交面板必须在收盘后保留最后数据',
)

assert.doesNotMatch(
  stockPanel,
  /min-w-max|min-w-\[1040px\]/,
  '盘口布局不能用固定总宽度撑破个股弹窗',
)
const dailyTransactionsStart = stockPanel.indexOf('<StockTransactionsPanel')
const dailyTransactionsEnd = stockPanel.indexOf('/>', dailyTransactionsStart)
const dailyTransactions = stockPanel.slice(dailyTransactionsStart, dailyTransactionsEnd)
assert.match(
  dailyTransactions,
  /height=\{height \+ LIVE_PANEL_GAP \+ LIVE_DEPTH_PANEL_HEIGHT\}/,
  '日 K 成交面板必须覆盖分时图、间距和五档盘口的总高度',
)
assert.match(
  stockPanel,
  /style=\{\{ height: LIVE_DEPTH_PANEL_HEIGHT \}\}/,
  '五档盘口和成交栏必须复用同一高度契约',
)

console.log('stock live-panel wiring verification passed')
