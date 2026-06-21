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
  signal_broken_limit_up: '炸板',
}

/** 内置信号 ID 列表 */
export const SIGNAL_OPTIONS = Object.keys(SIGNAL_LABELS)

/** 常用技术指标/字段 → 中文 (阈值条件展示用, 与后端 ENRICHED_COLUMNS 对齐) */
const FIELD_LABELS: Record<string, string> = {
  close: '收盘价', open: '开盘价', high: '最高价', low: '最低价',
  change_pct: '涨跌幅', change_amount: '涨跌额', amplitude: '振幅',
  turnover_rate: '换手率', volume: '成交量', amount: '成交额',
  ma5: 'MA5', ma10: 'MA10', ma20: 'MA20', ma30: 'MA30', ma60: 'MA60',
  ema5: 'EMA5', ema10: 'EMA10', ema20: 'EMA20',
  macd_dif: 'MACD-DIF', macd_dea: 'MACD-DEA', macd_hist: 'MACD柱',
  boll_upper: '布林上轨', boll_lower: '布林下轨',
  kdj_k: 'KDJ-K', kdj_d: 'KDJ-D', kdj_j: 'KDJ-J',
  rsi_6: 'RSI6', rsi_14: 'RSI14', rsi_24: 'RSI24',
  vol_ratio_5d: '5日量比', vol_ratio_20d: '20日量比',
  vol_ma5: '5日均量', vol_ma10: '10日均量',
  high_60d: '60日最高', low_60d: '60日最低',
  momentum_5d: '5日动量', momentum_20d: '20日动量', momentum_60d: '60日动量',
  atr_14: 'ATR14', annual_vol_20d: '20日年化波动',
  consecutive_limit_ups: '连板数', consecutive_limit_downs: '跌停连板',
}

/**
 * 信号/字段 ID → 中文显示名。
 * 内置信号查 SIGNAL_LABELS; csg_ 前缀查传入的自定义信号名称映射;
 * 技术指标查 FIELD_LABELS; 都找不到则原样返回。
 */
export function cnSignal(name: string, customNames?: Record<string, string>): string {
  if (customNames && name in customNames) return customNames[name]
  return SIGNAL_LABELS[name] ?? FIELD_LABELS[name] ?? name
}
