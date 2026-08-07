const API_BASE = import.meta.env.VITE_API_BASE ?? ''

export type MarketSummary = {
  market_id: string
  split: string
  start_time: number
  end_time: number
  rows: number
  winner: number | null
  btc_open_price: number | null
  has_features: boolean
  has_training: boolean
}

export type MarketDetail = MarketSummary & {
  series: { t: number; btc: number | null; up: number | null; down: number | null }[]
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
  markets: (split: string, limit = 50) =>
    json<{ split: string; count: number; markets: MarketSummary[] }>(`/api/markets?split=${split}&limit=${limit}`),
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
}

export function wsUrl(path: string): string {
  const base = API_BASE || `${window.location.protocol}//${window.location.host}`
  const u = new URL(path, base)
  u.protocol = u.protocol === 'https:' ? 'wss:' : 'ws:'
  return u.toString()
}

export function formatUsd(n: number | null | undefined, digits = 2): string {
  if (n == null || Number.isNaN(n)) return '—'
  return n.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits })
}

export function formatPct(p: number | null | undefined): string {
  if (p == null || Number.isNaN(p)) return '—'
  return `${Math.round(p * 100)}%`
}

export function formatWindow(startMs: number, endMs: number): string {
  const s = new Date(startMs)
  const e = new Date(endMs)
  const opts: Intl.DateTimeFormatOptions = { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }
  return `${s.toLocaleString(undefined, opts)} – ${e.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })}`
}
