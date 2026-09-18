/** Extract a generic chart annotation id from ECharts click params. */
export function chartInspectionId(params: unknown): string | null {
  if (!params || typeof params !== 'object') return null
  const data = (params as { data?: unknown }).data
  const candidate = Array.isArray(data)
    ? (data[0] as { inspectionId?: unknown } | undefined)?.inspectionId
    : (data as { inspectionId?: unknown } | null | undefined)?.inspectionId
  return typeof candidate === 'string' && candidate.length > 0 ? candidate : null
}
