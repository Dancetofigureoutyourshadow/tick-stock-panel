import { readFileSync } from 'node:fs'
import { expect, it } from 'vitest'

const source = readFileSync(
  new URL('./DimensionMembersDialogContent.tsx', import.meta.url),
  'utf8',
)

it('股票名称在前，板块标签紧随名称且代码保持独立一行', () => {
  // 只锚定结构（外层包裹 + 名称/徽标同行 flex 组 + 独立代码行），不锁定具体样式类，
  // 避免纯样式微调误报；顺序断言仍是本测试的核心。
  const identity = source.match(
    /<span className="min-w-0">\s*<span className="flex[^"]*">([\s\S]*?)<\/span>\s*<span className="block font-mono[^"]*">\{row\.symbol\}<\/span>/,
  )

  expect(identity, '股票名称、板块标签和代码应使用统一的两行身份布局').toBeTruthy()
  expect(
    identity![1].indexOf('{row.name || row.symbol}'),
    '板块标签应渲染在股票名称之后',
  ).toBeLessThan(identity![1].indexOf('{board &&'))
})
