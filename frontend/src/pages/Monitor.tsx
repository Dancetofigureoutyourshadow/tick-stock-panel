import { RadioTower } from 'lucide-react'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState } from '@/components/EmptyState'

// 后续实现计划(本轮为占位):
//
// 一、信号来源
//   1. 策略告警 —— 链路已存在(StrategyMonitorService → quote_service → SSE),
//      后续接入持久化即可直接进入本列表。
//   2. 全市场涨跌停异动 —— 涨停/跌停/炸板(signal_broken_limit_up)/翘板
//      (signal_limit_down_recovery)指标已在 enriched 全量计算,补一条
//      全市场扫描链路即可产出告警。
//   3. 板块异动 —— 当前后端无独立模块,需从零开发板块聚合异动检测。
//
// 二、历史持久化
//   告警落盘到 data/user_data/alerts.jsonl(追加写,保留近 7 天 / 上限约
//   5000 条),支持按来源/类型过滤、一键清空。当前告警为 fire-and-forget,
//   刷新即丢失,持久化后将形成真正的「监控列表」。
//
// 三、通知通道扩展(预留)
//   StrategyMonitorService 已预留 alert_handler 扩展点,后续可接入飞书
//   webhook、邮件、短信等外部通知通道。
const PLAN: { title: string; desc: string }[] = [
  {
    title: '策略告警',
    desc: '链路已存在(StrategyMonitorService → quote_service → SSE),后续接入持久化即可直接进入本列表。',
  },
  {
    title: '涨跌停异动',
    desc: '涨停 / 跌停 / 炸板 / 翘板指标已在 enriched 全量计算,补一条全市场扫描链路即可产出告警。',
  },
  {
    title: '板块异动',
    desc: '当前后端无独立模块,需从零开发板块聚合异动检测逻辑。',
  },
  {
    title: '历史持久化',
    desc: '告警落盘 data/user_data/alerts.jsonl(追加写,保留近 7 天 / 上限约 5000 条),支持按来源/类型过滤、清空。',
  },
  {
    title: '通知通道',
    desc: 'StrategyMonitorService 已预留 alert_handler 扩展点,后续可接入飞书 webhook、邮件、短信。',
  },
]

export function Monitor() {
  return (
    <div className="flex flex-col h-full">
      <PageHeader title="监控通知" subtitle="实时信号中心 · 开发中" />

      <div className="flex-1 overflow-auto px-5 py-6">
        <div className="max-w-3xl mx-auto">
          <EmptyState
            icon={RadioTower}
            title="监控通知开发中"
            hint="本页面将汇聚实时监控产生的全部信号:策略触发的买卖提醒、全市场涨跌停异动、板块异动等。当前为占位页面,下方为后续实现规划。"
          />

          <section className="mt-6 rounded-card border border-border bg-surface p-5">
            <h3 className="text-sm font-semibold text-foreground">后续实现规划</h3>
            <ul className="mt-3 space-y-3">
              {PLAN.map((item) => (
                <li key={item.title} className="flex gap-3">
                  <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-accent" />
                  <div>
                    <p className="text-sm font-medium text-foreground">{item.title}</p>
                    <p className="mt-0.5 text-xs leading-relaxed text-secondary">{item.desc}</p>
                  </div>
                </li>
              ))}
            </ul>
          </section>
        </div>
      </div>
    </div>
  )
}
