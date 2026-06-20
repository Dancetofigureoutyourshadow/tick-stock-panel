/**
 * 买卖触发器信号定义 — 选股页弹窗 / 回测页共用。
 *
 * 信号 ID 必须与后端 backtest/strategy.py:_build_signal_mask 对齐
 * (signal_* 前缀为内置原子信号, csg_ 前缀为用户自定义信号)。
 */

/** 内置原子信号 → 中文标签 (权威来源, 两页统一) */
export const SIGNAL_LABELS: Record<string, string> = {
  signal_ma_golden_5_20: 'MA5上穿MA20',
  signal_ma_dead_5_20: 'MA5下穿MA20',
  signal_ma_golden_20_60: 'MA20上穿MA60',
  signal_macd_golden: 'MACD金叉',
  signal_macd_dead: 'MACD死叉',
  signal_ma20_breakout: '突破MA20',
  signal_ma20_breakdown: '跌破MA20',
  signal_n_day_high: '60日新高',
  signal_n_day_low: '60日新低',
  signal_boll_breakout_upper: '突破布林上轨',
  signal_boll_breakdown_lower: '跌破布林下轨',
  signal_volume_surge: '放量',
  signal_limit_up: '涨停',
  signal_limit_down: '跌停',
  signal_limit_down_recovery: '跌停翘板',
  signal_broken_board_recovery: '断板反包',
}

/** 内置信号 ID 列表 */
export const SIGNAL_OPTIONS = Object.keys(SIGNAL_LABELS)

/**
 * 信号 ID → 中文显示名。
 * 内置信号查 SIGNAL_LABELS; csg_ 前缀查传入的自定义信号名称映射, 找不到则原样返回。
 */
export function cnSignal(name: string, customNames?: Record<string, string>): string {
  if (customNames && name in customNames) return customNames[name]
  return SIGNAL_LABELS[name] ?? name
}
