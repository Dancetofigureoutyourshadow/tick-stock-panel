import { useMemo } from 'react'
import { useECharts } from './useECharts'
import type { EChartsOption } from 'echarts'

interface DistBin {
  range: string
  count: number
  ratio: number
}

/**
 * 收益分布直方图 — 全量模拟专用的候选标的收益分布。
 * 柱子颜色按收益正负区分(正红负绿),零轴居中。
 */
export function ReturnDistributionChart({ distribution }: { distribution: DistBin[] }) {
  const option = useMemo<EChartsOption>(() => {
    const cats = distribution.map(d => d.range)
    const vals = distribution.map(d => d.count)
    // 判断每档是正还是负(按 range 字符串首字符 +/~)
    const colors = distribution.map(d => {
      const lo = parseFloat(d.range)
      // 中心档(跨 0) 用中性色
      if (lo < 0 && parseFloat(d.range.split('~')[1]) > 0) return '#a1a1aa'
      return lo >= 0 ? '#ef4444' : '#22c55e'
    })

    return {
      grid: { left: 48, right: 16, top: 24, bottom: 56 },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        formatter: (params: any) => {
          const p = Array.isArray(params) ? params[0] : params
          const bin = distribution[p.dataIndex]
          if (!bin) return ''
          return `${bin.range}<br/>数量: ${bin.count}<br/>占比: ${(bin.ratio * 100).toFixed(1)}%`
        },
      },
      xAxis: {
        type: 'category',
        data: cats,
        axisLabel: { color: '#a1a1aa', fontSize: 10, rotate: 45, interval: 1 },
        axisLine: { lineStyle: { color: '#3f3f46' } },
      },
      yAxis: {
        type: 'value',
        axisLabel: { color: '#a1a1aa', fontSize: 10 },
        splitLine: { lineStyle: { color: '#27272a' } },
      },
      series: [
        {
          type: 'bar',
          data: vals.map((v, i) => ({ value: v, itemStyle: { color: colors[i] } })),
          barWidth: '90%',
        },
      ],
    }
  }, [distribution])

  const chartRef = useECharts(option, [distribution])

  return <div ref={chartRef} className="h-48 w-full" />
}
