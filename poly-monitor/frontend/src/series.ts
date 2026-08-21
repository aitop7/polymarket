/** Shared market series keys (BTC 5m/15m + BNB 15m). */

export type MarketSeriesKey = '5m' | '15m' | 'bnb-15m'

export const SERIES_WINDOW_MS: Record<MarketSeriesKey, number> = {
  '5m': 300_000,
  '15m': 900_000,
  'bnb-15m': 900_000,
}

export const SERIES_STORAGE_KEY = 'poly_monitor_series'

export const UPDOWN_SLUG_RE = /^(btc|bnb)-updown-(5m|15m)-(\d+)$/i

export function isMarketSeriesKey(v: string | null | undefined): v is MarketSeriesKey {
  return v === '5m' || v === '15m' || v === 'bnb-15m'
}

export function loadMarketSeries(): MarketSeriesKey {
  try {
    const v = sessionStorage.getItem(SERIES_STORAGE_KEY)
    if (isMarketSeriesKey(v)) return v
  } catch {
    /* ignore */
  }
  return '5m'
}

export function saveMarketSeries(s: MarketSeriesKey): void {
  try {
    sessionStorage.setItem(SERIES_STORAGE_KEY, s)
  } catch {
    /* ignore */
  }
}

export function seriesAsset(s: MarketSeriesKey): 'BTC' | 'BNB' {
  return s === 'bnb-15m' ? 'BNB' : 'BTC'
}

export function seriesBinanceSymbol(s: MarketSeriesKey): string {
  return s === 'bnb-15m' ? 'BNBUSDT' : 'BTCUSDT'
}

export function seriesLabel(s: MarketSeriesKey): string {
  if (s === 'bnb-15m') return 'BNB 15m'
  if (s === '15m') return 'BTC 15m'
  return 'BTC 5m'
}

export function seriesTitle(s: MarketSeriesKey): string {
  if (s === 'bnb-15m') return 'BNB Up or Down 15m'
  if (s === '15m') return 'BTC Up or Down 15m'
  return 'BTC Up or Down 5m'
}

export function seriesDurationLabel(s: MarketSeriesKey): string {
  return s === '5m' ? '5m' : '15m'
}
