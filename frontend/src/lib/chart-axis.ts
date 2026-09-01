export function formatPriceAxisLabel(value: unknown): string {
  const price = Number(value)
  if (!Number.isFinite(price)) return String(value)
  return price.toFixed(Math.abs(price) < 1 ? 3 : 2).replace(/\.?0+$/, '')
}
