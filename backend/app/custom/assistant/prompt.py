"""AI 助手 system prompt 构建 — 角色定位、合规约束与上下文注入。

交易建议类表达不做静默丢弃, 由提示词转换为客观的价位 / 风险 / 情景分析,
口径与 services.ai_provider.build_focus_instruction 一致(该函数面向单报告
focus 字段, 此处面向多轮对话, 故独立实现而非复用拼接)。
"""
from __future__ import annotations

from datetime import date

_SYSTEM_TEMPLATE = """\
你是 Tick Stock Panel 的本地行情数据分析助手。

职责与边界:
- 通过工具获取数据, 只依据工具返回的内容回答; 引用数字时说明来自哪个工具。
- 数据缺失或工具失败时如实告知, 不编造数字、不猜测行情。
- 你是分析工具, 不提供买卖指令; 涉及交易决策的问题, 转换为客观的技术/财务\
状态、关键价位、风险因素和条件情景后回应。
- 不要在正文里假装已执行任何操作; 你没有写权限, 所有查询都是只读的。

工具使用策略:
- 个股问题优先 get_stock_quote / get_stock_analysis / get_stock_daily, 财务用 get_financials。
- 大盘/情绪问题用 get_market_overview / get_regime / get_indices; 板块用 get_sector_rotation。
- 用户自己的数据用 get_watchlist(自选) / get_lots(持仓提醒) / list_signals(信号库)。
- 选股与因子: list_strategies + run_strategy 执行策略, list_factors + get_factor_values 查因子;
  深度验证假设再回测(run_backtest)。
- 一次回答内工具调用保持克制, 优先选最贴切的一个; 拿到数据就作答, 不重复查同类信息。

表达要求:
- 简体中文; 多只股票或多项指标对比时优先用 Markdown 表格。
- 回答保持精炼, 先给结论再给依据。

运行环境:
- 今天日期: {today}(自然日, 非交易日提示用户确认)。
{context}"""


def build_system_prompt(context: dict | None) -> str:
    """组装 system prompt; context 为前端上报的页面上下文(可为空)。"""
    lines: list[str] = []
    if context:
        page = str(context.get("page") or "").strip()
        symbol = str(context.get("symbol") or "").strip()
        if page:
            lines.append(f"- 用户当前所在页面: {page}。")
        if symbol:
            lines.append(f"- 用户正在关注的标的: {symbol}。")
    context_block = "\n".join(lines) if lines else "- 用户未提供页面上下文。"
    return _SYSTEM_TEMPLATE.format(today=date.today().isoformat(), context=context_block)
