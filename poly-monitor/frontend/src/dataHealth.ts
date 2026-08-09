/**
 * Re-exports thresholds from poly-monitor/shared/data_health.json
 * (single source for frontend + backend). Edit that JSON file only.
 */
import thresholds from '../../shared/data_health.json'

export const PRICE_HEALTH_GREAT_MS = thresholds.price.great_ms
export const PRICE_HEALTH_GOOD_MS = thresholds.price.good_ms
export const PRICE_HEALTH_OK_MS = thresholds.price.ok_ms
export const PRICE_HEALTH_LOW_MS = thresholds.price.low_ms

export const TRADE_HEALTH_GREAT_MS = thresholds.trade.great_ms
export const TRADE_HEALTH_GOOD_MS = thresholds.trade.good_ms
export const TRADE_HEALTH_OK_MS = thresholds.trade.ok_ms
export const TRADE_HEALTH_LOW_MS = thresholds.trade.low_ms

function secLabel(ms: number): string {
  const s = ms / 1000
  return Number.isInteger(s) ? String(s) : s.toFixed(1)
}

export function healthThresholdHeadline(
  tone: 'great' | 'good' | 'ok' | 'low' | 'bad' | 'unchecked',
): string {
  if (tone === 'great') {
    return `Great — price ≤${secLabel(PRICE_HEALTH_GREAT_MS)}s · trades <${secLabel(TRADE_HEALTH_GREAT_MS)}s`
  }
  if (tone === 'good') {
    return `Good — price ≤${secLabel(PRICE_HEALTH_GOOD_MS)}s · trades <${secLabel(TRADE_HEALTH_GOOD_MS)}s`
  }
  if (tone === 'ok') {
    return `Ok — price ≤${secLabel(PRICE_HEALTH_OK_MS)}s · trades <${secLabel(TRADE_HEALTH_OK_MS)}s`
  }
  if (tone === 'low') {
    return `Low — price ≤${secLabel(PRICE_HEALTH_LOW_MS)}s · trades <${secLabel(TRADE_HEALTH_LOW_MS)}s`
  }
  if (tone === 'bad') {
    return `Bad — price >${secLabel(PRICE_HEALTH_LOW_MS)}s · trades ≥${secLabel(TRADE_HEALTH_LOW_MS)}s`
  }
  return 'Data not checked yet'
}
