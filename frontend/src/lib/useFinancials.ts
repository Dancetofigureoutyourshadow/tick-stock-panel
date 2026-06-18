import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'

export const FINANCIAL_QK = {
  status: ['financials', 'status'],
  metrics: (symbol?: string) => ['financials', 'metrics', symbol],
  income: (symbol?: string) => ['financials', 'income', symbol],
  balanceSheet: (symbol?: string) => ['financials', 'balance-sheet', symbol],
  cashFlow: (symbol?: string) => ['financials', 'cash-flow', symbol],
}

export function useFinancialStatus() {
  return useQuery({
    queryKey: FINANCIAL_QK.status,
    queryFn: () => api.financialStatus(),
    staleTime: 60_000,
  })
}

export function useFinancialMetrics(symbol?: string) {
  return useQuery({
    queryKey: FINANCIAL_QK.metrics(symbol),
    queryFn: () => api.financialMetrics(symbol),
    enabled: !!symbol,
    staleTime: 300_000,
  })
}

export function useFinancialSync() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (table: string) => api.financialSync(table),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: FINANCIAL_QK.status })
      qc.invalidateQueries({ queryKey: ['financials'] })
    },
  })
}
