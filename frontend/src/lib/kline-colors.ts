export const LIMIT_UP_COLOR = '#FACC15'
export const LIMIT_DOWN_COLOR = '#8B5CF6'

export interface KlineLimitFlags {
  signal_limit_up?: boolean | null
  signal_limit_down?: boolean | null
}

/** 返回已由后端按原始价格和交易日规则计算的涨跌停颜色。 */
export function getKlineLimitColor(row: KlineLimitFlags): string | undefined {
  if (row.signal_limit_up === true) return LIMIT_UP_COLOR
  if (row.signal_limit_down === true) return LIMIT_DOWN_COLOR
  return undefined
}
