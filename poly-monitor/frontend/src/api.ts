const API_BASE = import.meta.env.VITE_API_BASE ?? ''

export type MarketSummary = {
  market_id: string
  split: string
  start_time: number
  end_time: number
  rows: number
  winner: number | null
  /** From meta.json for TWAP/live; true when market resolved/closed. */
  closed?: boolean | null
  btc_open_price: number | null
  has_features: boolean
  has_training: boolean
  date_et?: string
  time_et?: string
}

export type MarketDetail = MarketSummary & {
  series: {
    t: number
    btc: number | null
    up: number | null
    down: number | null
    twap?: number | null
    chainlink?: number | null
  }[]
  first: { timestamp: number; btc_price: number | null; up_price: number; down_price: number }
  last: { timestamp: number; btc_price: number | null; up_price: number; down_price: number }
}

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) },
    ...init,
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(text || res.statusText)
  }
  return res.json() as Promise<T>
}

export const api = {
  health: () => json<{ ok: boolean }>('/api/health'),
  strategies: () => json<{ name: string; description: string; params: Record<string, unknown> }[]>('/api/strategies'),
  markets: (split: string, opts?: { limit?: number; date?: string; rebuild_index?: boolean }) => {
    const q = new URLSearchParams({ split })
    if (opts?.limit != null) q.set('limit', String(opts.limit))
    if (opts?.date) q.set('date', opts.date)
    if (opts?.rebuild_index) q.set('rebuild_index', 'true')
    return json<{ split: string; count: number; markets: MarketSummary[]; date?: string }>(`/api/markets?${q}`)
  },
  marketDates: (split: string, opts?: { rebuild_index?: boolean }) => {
    const q = new URLSearchParams({ split })
    if (opts?.rebuild_index) q.set('rebuild_index', 'true')
    return json<{ split: string; count: number; dates: string[]; min: string | null; max: string | null }>(
      `/api/markets/dates?${q}`,
    )
  },
  marketAt: (split: string, opts: { date?: string; time?: string; t?: number }) => {
    const q = new URLSearchParams({ split })
    if (opts.date) q.set('date', opts.date)
    if (opts.time) q.set('time', opts.time)
    if (opts.t != null) q.set('t', String(opts.t))
    return json<MarketSummary>(`/api/markets/at?${q}`)
  },
  market: (id: string, split?: string) =>
    json<MarketDetail>(`/api/markets/${id}${split ? `?split=${split}` : ''}`),
  neighbors: (id: string, split?: string) =>
    json<{ prev: string | null; next: string | null; split: string; index: number; total: number }>(
      `/api/markets/${id}/neighbors${split ? `?split=${split}` : ''}`,
    ),
  book: (id: string, t?: number) =>
    json<Record<string, unknown>>(`/api/markets/${id}/book${t != null ? `?t=${t}` : ''}`),
  backtest: (body: Record<string, unknown>) =>
    json<Record<string, unknown>>('/api/backtest', { method: 'POST', body: JSON.stringify(body) }),
  paperSession: (body: Record<string, unknown>) =>
    json<{ session_id: string; market_id: string; rows: number; speed: number }>(
      '/api/paper/session',
      { method: 'POST', body: JSON.stringify(body) },
    ),
  paperOrder: (body: Record<string, unknown>) =>
    json<{ ok: boolean }>('/api/paper/order', { method: 'POST', body: JSON.stringify(body) }),
  paperStatus: (sessionId: string) => json<Record<string, unknown>>(`/api/paper/${sessionId}`),
  liveState: () => json<LiveTick>('/api/live/state'),
  liveSeries: (marketId?: string | null, lookbackMs = 300_000) => {
    const q = new URLSearchParams()
    if (marketId) q.set('market_id', marketId)
    q.set('lookback_ms', String(lookbackMs))
    const qs = q.toString()
    return json<LiveSeriesResponse>(`/api/live/series${qs ? `?${qs}` : ''}`)
  },
  liveHolders: (limit = 20) => {
    const q = new URLSearchParams({
      limit: String(Math.max(1, Math.min(20, limit))),
      _ts: String(Date.now()),
    })
    return json<LiveHoldersResponse>(`/api/live/holders?${q}`, {
      cache: 'no-store',
    })
  },
}

export type LiveSeriesPoint = {
  t: number
  up?: number | null
  down?: number | null
  btc?: number | null
  twap?: number | null
  chainlink?: number | null
}

export type LiveSeriesResponse = {
  market_id?: string | null
  start_time?: number | null
  end_time?: number | null
  series: LiveSeriesPoint[]
  source?: { parquet?: number; twap_feed?: number; buffer?: number }
}

export type HolderRow = {
  proxy_wallet: string
  display_name: string
  amount: number
  profile_image?: string
  verified?: boolean
  outcome_index?: number | null
}

export type LiveHoldersResponse = {
  market_id?: string | null
  condition_id?: string | null
  updated_at?: number
  live?: boolean
  up: HolderRow[]
  down: HolderRow[]
}

export type LiveTick = {
  type: string
  live?: boolean
  timestamp: number
  market_id?: string | null
  slug?: string | null
  start_time?: number | null
  end_time?: number | null
  btc_price?: number | null
  price_to_beat?: number | null
  btc_open?: number | null
  btc_twap_30s?: number | null
  btc_chainlink?: number | null
  up_price?: number
  down_price?: number
  remaining_seconds?: number
  elapsed_seconds?: number
  book?: Record<string, unknown>
  error?: string
  message?: string
}

export function wsUrl(path: string): string {
  const base = API_BASE || `${window.location.protocol}//${window.location.host}`
  const u = new URL(path, base)
  u.protocol = u.protocol === 'https:' ? 'wss:' : 'ws:'
  return u.toString()
}

function isWholeAfterDigits(n: number, digits: number): boolean {
  const factor = 10 ** digits
  const rounded = Math.round(Math.abs(n) * factor) / factor
  return Math.abs(rounded - Math.round(rounded)) < 1e-12
}

/** Drop trailing zeros / decimal when the fractional part is zero (e.g. 12.00 → 12). */
function formatFixedTrim(n: number, digits: number): string {
  if (isWholeAfterDigits(n, digits)) return String(Math.round(n))
  return n.toFixed(digits)
}

export function formatUsd(n: number | null | undefined, digits = 2): string {
  if (n == null || Number.isNaN(n)) return '—'
  const minFrac = isWholeAfterDigits(n, digits) ? 0 : digits
  return n.toLocaleString(undefined, {
    minimumFractionDigits: minFrac,
    maximumFractionDigits: digits,
  })
}

/** Format probability/price as cents with up to 2 fractional digits (e.g. 51.48¢, 51¢). */
export function formatCents(p: number | null | undefined): string {
  if (p == null || Number.isNaN(p)) return '—¢'
  return `${formatFixedTrim(p * 100, 2)}¢`
}

/** Format probability/price as whole cents (e.g. 51¢). */
export function formatCentsInt(p: number | null | undefined): string {
  if (p == null || Number.isNaN(p)) return '—¢'
  return `${Math.round(p * 100)}¢`
}

/**
 * Trade / order-book display: whole cents mid-range, one decimal at the extremes
 * (≤1¢ or ≥90¢) where Polymarket uses finer ticks. Whole values stay integers (no .0).
 */
export function formatCentsTrade(p: number | null | undefined): string {
  if (p == null || Number.isNaN(p)) return '—¢'
  const c = p * 100
  if (c <= 1 || c >= 90) {
    return `${formatFixedTrim(c, 1)}¢`
  }
  return `${Math.round(c)}¢`
}

function formatCentsBound(cents: number): string {
  const c = Math.max(0, Math.min(100, cents))
  if (c <= 1 || c >= 90) {
    return formatFixedTrim(c, 1)
  }
  return String(Math.round(c))
}

/** Same adaptive rule as formatCentsTrade, without the ¢ suffix (for ranges). */
export function formatCentsTradeNumber(p: number): string {
  return formatCentsBound(p * 100)
}

export function formatPct(p: number | null | undefined): string {
  if (p == null || Number.isNaN(p)) return '—'
  return `${(p * 100).toFixed(2)}%`
}

export function formatWindow(startMs: number, endMs: number): string {
  const s = new Date(startMs)
  const e = new Date(endMs)
  const opts: Intl.DateTimeFormatOptions = {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  }
  return `${s.toLocaleString(undefined, opts)} – ${e.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })}`
}

/** Polymarket-style window: "August 7, 12:50-12:55AM ET" */
export function formatWindowEt(startMs: number, endMs: number): string {
  const optsDate: Intl.DateTimeFormatOptions = {
    timeZone: 'America/New_York',
    month: 'long',
    day: 'numeric',
  }
  const optsTime: Intl.DateTimeFormatOptions = {
    timeZone: 'America/New_York',
    hour: 'numeric',
    minute: '2-digit',
    hour12: true,
  }
  const s = new Date(startMs)
  const e = new Date(endMs)
  const date = new Intl.DateTimeFormat('en-US', optsDate).format(s)
  const t0 = new Intl.DateTimeFormat('en-US', optsTime).format(s).replace(' ', '')
  const t1 = new Intl.DateTimeFormat('en-US', optsTime).format(e).replace(' ', '')
  // Collapse duplicate AM/PM when same meridian
  const ampm = (t: string) => (t.toUpperCase().includes('PM') ? 'PM' : 'AM')
  let range: string
  if (ampm(t0) === ampm(t1)) {
    const startNoMer = t0.replace(/\s?(AM|PM)/i, '')
    range = `${startNoMer}-${t1}`
  } else {
    range = `${t0}-${t1}`
  }
  return `${date}, ${range} ET`
}

/** Compact label for selects: "Jul 8, 2026, 8:00–8:05 PM" */
export function formatMarketLabel(startMs: number, endMs: number, marketId?: string): string {
  const s = new Date(startMs)
  const e = new Date(endMs)
  const date = s.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
  const t0 = s.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })
  const t1 = e.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })
  const window = `${date}, ${t0} – ${t1}`
  return marketId ? `${window} · ${marketId}` : window
}
