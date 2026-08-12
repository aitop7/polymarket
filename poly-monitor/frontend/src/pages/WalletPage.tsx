import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import {
  api,
  formatCents,
  formatUsd,
  type WalletDailyRow,
  type WalletMarketActivity,
  type WalletMarketPnl,
  type WalletPnlInterval,
  type WalletPnlResponse,
  type WalletSummary,
} from '../api'

const INTERVALS: { id: WalletPnlInterval; label: string; subtitle: string }[] = [
  { id: '1d', label: '1D', subtitle: 'Past Day' },
  { id: '1w', label: '1W', subtitle: 'Past Week' },
  { id: '1m', label: '1M', subtitle: 'Past Month' },
  { id: '1y', label: '1Y', subtitle: 'Past Year' },
  { id: 'ytd', label: 'YTD', subtitle: 'Year to Date' },
  { id: 'all', label: 'ALL', subtitle: 'All Time' },
]

const ADDR_RE = /^0x[a-fA-F0-9]{40}$/

function todayEt(): string {
  try {
    return new Intl.DateTimeFormat('en-CA', {
      timeZone: 'America/New_York',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
    }).format(new Date())
  } catch {
    return new Date().toISOString().slice(0, 10)
  }
}

function shorten(addr: string): string {
  const s = addr.trim()
  if (s.length <= 12) return s
  return `${s.slice(0, 6)}…${s.slice(-4)}`
}

function fmtSignedUsd(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return '—'
  const sign = n > 0 ? '+' : n < 0 ? '−' : ''
  return `${sign}$${formatUsd(Math.abs(n))}`
}

function fmtTimeShort(ms: number): string {
  try {
    return new Intl.DateTimeFormat('en-US', {
      timeZone: 'America/New_York',
      hour: 'numeric',
      minute: '2-digit',
      second: '2-digit',
      hour12: true,
    }).format(new Date(ms))
  } catch {
    return new Date(ms).toLocaleTimeString()
  }
}

function formatCompactUsd(n: number): string {
  if (!Number.isFinite(n)) return '—'
  const abs = Math.abs(n)
  if (abs >= 1000) return `${n < 0 ? '−' : ''}${(abs / 1000).toFixed(1)}k`
  return formatUsd(n)
}

function fmtChartTick(ms: number, interval: WalletPnlInterval): string {
  const d = new Date(ms)
  if (interval === '1d') {
    return d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })
  }
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

/** "Bitcoin Up or Down - August 12, 12:05PM-12:10PM ET" → "12:05PM–12:10PM" */
function shortMarketLabel(title?: string | null, slug?: string | null): string {
  const t = (title || '').trim()
  if (t) {
    const window = t.match(/(\d{1,2}:\d{2}\s*[AP]M)\s*-\s*(\d{1,2}:\d{2}\s*[AP]M)/i)
    if (window) {
      return `${window[1].replace(/\s+/g, '')}–${window[2].replace(/\s+/g, '')}`
    }
    const stripped = t.replace(/^Bitcoin\s+Up\s+or\s+Down\s*[-–—:]?\s*/i, '').trim()
    if (stripped) return stripped
  }
  const s = (slug || '').trim()
  if (/^btc-updown-5m-\d+$/i.test(s)) {
    const startSec = Number(s.slice('btc-updown-5m-'.length))
    if (Number.isFinite(startSec) && startSec > 0) {
      const start = new Date(startSec * 1000)
      const end = new Date((startSec + 300) * 1000)
      const fmt = (d: Date) =>
        d
          .toLocaleTimeString('en-US', {
            timeZone: 'America/New_York',
            hour: 'numeric',
            minute: '2-digit',
            hour12: true,
          })
          .replace(/\s+/g, '')
      return `${fmt(start)}–${fmt(end)}`
    }
  }
  return t || s || '—'
}

export default function WalletPage() {
  const { walletAddress: walletParam } = useParams<{ walletAddress?: string }>()
  const navigate = useNavigate()
  const [query, setQuery] = useState(walletParam || '')
  const [wallet, setWallet] = useState<string | null>(null)
  const [date, setDate] = useState(todayEt())
  const [interval, setInterval] = useState<WalletPnlInterval>('1d')

  const [summary, setSummary] = useState<WalletSummary | null>(null)
  const [pnl, setPnl] = useState<WalletPnlResponse | null>(null)
  const [daily, setDaily] = useState<WalletDailyRow[]>([])
  const [activityMarkets, setActivityMarkets] = useState<WalletMarketActivity[]>([])
  const [marketPnls, setMarketPnls] = useState<WalletMarketPnl[]>([])
  const [marketsTotalPnl, setMarketsTotalPnl] = useState<number | null>(null)
  const [expandedMarket, setExpandedMarket] = useState<string | null>(null)

  const [loading, setLoading] = useState(false)
  const [pnlLoading, setPnlLoading] = useState(false)
  const [activityLoading, setActivityLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const loadWalletData = async (raw: string) => {
    const addr = raw.trim()
    if (!ADDR_RE.test(addr)) {
      setError('Enter a valid wallet address (0x + 40 hex chars)')
      return
    }
    const normalized = addr.toLowerCase()
    setLoading(true)
    setError(null)
    setWallet(normalized)
    setQuery(normalized)
    try {
      const [sum, dailyRes] = await Promise.all([
        api.walletSummary(normalized),
        api.walletDaily(normalized, 120),
      ])
      setSummary(sum)
      const days = dailyRes.daily || []
      setDaily(days)
      if (days.length) {
        setDate(days[0].date)
      }
    } catch (e) {
      setSummary(null)
      setDaily([])
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  const goToWallet = (raw: string) => {
    const addr = raw.trim()
    if (!ADDR_RE.test(addr)) {
      setError('Enter a valid wallet address (0x + 40 hex chars)')
      return
    }
    const normalized = addr.toLowerCase()
    if (walletParam?.toLowerCase() === normalized) {
      void loadWalletData(normalized)
      return
    }
    navigate(`/wallet/${normalized}`)
  }

  useEffect(() => {
    if (!walletParam) {
      setWallet(null)
      setSummary(null)
      setDaily([])
      setActivityMarkets([])
      setMarketPnls([])
      setMarketsTotalPnl(null)
      setPnl(null)
      setExpandedMarket(null)
      setError(null)
      return
    }
    if (!ADDR_RE.test(walletParam)) {
      setError('Invalid wallet address in URL')
      setWallet(null)
      return
    }
    void loadWalletData(walletParam)
    // eslint-disable-next-line react-hooks/exhaustive-deps -- reload when URL wallet changes
  }, [walletParam])

  useEffect(() => {
    if (!wallet) {
      setPnl(null)
      return
    }
    let cancelled = false
    setPnlLoading(true)
    api
      .walletPnl(wallet, interval)
      .then((res) => {
        if (!cancelled) setPnl(res)
      })
      .catch((e) => {
        if (!cancelled) {
          setPnl(null)
          setError(e instanceof Error ? e.message : String(e))
        }
      })
      .finally(() => {
        if (!cancelled) setPnlLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [wallet, interval])

  useEffect(() => {
    if (!wallet || !date) {
      setActivityMarkets([])
      setMarketPnls([])
      setMarketsTotalPnl(null)
      return
    }
    let cancelled = false
    setActivityLoading(true)
    setExpandedMarket(null)
    Promise.all([
      api.walletActivity(wallet, { date, limit: 500 }),
      api.walletMarkets(wallet, { date, limit: 200 }),
    ])
      .then(([act, mkts]) => {
        if (cancelled) return
        setActivityMarkets(act.markets || [])
        setMarketPnls(mkts.markets || [])
        setMarketsTotalPnl(mkts.total_pnl ?? null)
        const firstKey =
          (mkts.markets?.[0]?.condition_id ||
            mkts.markets?.[0]?.slug ||
            mkts.markets?.[0]?.title ||
            act.markets?.[0]?.condition_id ||
            act.markets?.[0]?.slug ||
            act.markets?.[0]?.title) ??
          null
        setExpandedMarket(firstKey)
      })
      .catch((e) => {
        if (!cancelled) {
          setActivityMarkets([])
          setMarketPnls([])
          setMarketsTotalPnl(null)
          setError(e instanceof Error ? e.message : String(e))
        }
      })
      .finally(() => {
        if (!cancelled) setActivityLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [wallet, date])

  const chartData = useMemo(() => {
    const series = pnl?.series || []
    if (!series.length) return []
    const base = series[0].pnl
    return series.map((p) => ({
      t: p.t,
      pnl: p.pnl,
      delta: p.pnl - base,
    }))
  }, [pnl])

  const selectedMarketActivity = useMemo(() => {
    if (!expandedMarket) return null
    return (
      activityMarkets.find((m) => {
        const key = m.condition_id || m.slug || m.title || 'unknown'
        return key === expandedMarket
      }) ?? null
    )
  }, [activityMarkets, expandedMarket])

  const selectedMarketMeta = useMemo(() => {
    if (!expandedMarket) return null
    return (
      marketPnls.find((m) => {
        const key = m.condition_id || m.slug || m.title || 'm'
        return key === expandedMarket
      }) ?? null
    )
  }, [marketPnls, expandedMarket])

  const boughtShares = useMemo(() => {
    const rows = selectedMarketActivity?.activity || []
    let up = 0
    let down = 0
    for (const row of rows) {
      const side = (row.side || '').toUpperCase()
      if (side === 'SELL') continue
      // Treat missing side as buy for TRADE activity rows
      if (side && side !== 'BUY') continue
      const outcome = (row.outcome || '').toLowerCase()
      const shares = Number(row.shares) || 0
      if (outcome === 'up') up += shares
      else if (outcome === 'down') down += shares
    }
    return { up, down }
  }, [selectedMarketActivity])

  const intervalMeta = INTERVALS.find((x) => x.id === interval) || INTERVALS[0]
  const pnlValue = pnl?.pnl ?? null
  const pnlPositive = (pnlValue ?? 0) >= 0

  return (
    <div className="workspace wallet-workspace">
      <aside className="workspace-rail workspace-rail-left">
        <div className="control-sidebar control-sidebar-embedded">
          <div className="sidebar-section">
            <div className="sidebar-heading mode-heading">
              <span>Wallet</span>
              <span className="mode-current-pill">Activity</span>
            </div>
            <p className="muted" style={{ margin: '0 0 0.55rem', fontSize: '0.78rem', lineHeight: 1.4 }}>
              BTC Up/Down 5m only. Activity links open Polygonscan and Orbscan.
            </p>

            <label className="sidebar-label">Wallet address</label>
            <input
              type="text"
              spellCheck={false}
              placeholder="0x…"
              value={query}
              disabled={loading}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') goToWallet(query)
              }}
            />
            <button
              type="button"
              className="sidebar-btn primary"
              style={{ width: '100%', marginTop: '0.55rem' }}
              disabled={loading}
              onClick={() => goToWallet(query)}
            >
              {loading ? 'Loading…' : 'Search'}
            </button>
            <p className="muted" style={{ margin: '0.55rem 0 0', fontSize: '0.72rem', lineHeight: 1.35 }}>
              Pick a day in <strong>PnL by day</strong> to load that date’s markets.
            </p>
          </div>

          {summary && (
            <div className="sidebar-section sidebar-section-last">
              <div className="sidebar-heading">Links</div>
              <a className="wallet-ext-link" href={summary.polymarket_url} target="_blank" rel="noreferrer">
                Polymarket profile
              </a>
              <a className="wallet-ext-link" href={summary.orbscan_url} target="_blank" rel="noreferrer">
                Orbscan profile
              </a>
              <a className="wallet-ext-link" href={summary.polygonscan_url} target="_blank" rel="noreferrer">
                Polygonscan address
              </a>
            </div>
          )}
        </div>
      </aside>

      <div className="workspace-main wallet-main">
        {error && <p className="error">{error}</p>}

        {!wallet && !error && (
          <p className="muted" style={{ marginTop: '1rem' }}>
            Search a wallet address to load PnL and daily activity.
          </p>
        )}

        {summary && (
          <section className="wallet-hero-row">
            <div className="wallet-profile-card">
              <div className="wallet-profile-top">
                <div className="wallet-avatar" aria-hidden>
                  {summary.profile_image ? (
                    <img src={summary.profile_image} alt="" />
                  ) : (
                    (summary.name || '?').slice(0, 1).toUpperCase()
                  )}
                </div>
                <div className="wallet-profile-text">
                  <div className="wallet-profile-name">{summary.name}</div>
                  <div className="wallet-profile-addr" title={summary.wallet}>
                    {shorten(summary.wallet)}
                  </div>
                </div>
              </div>
              <div className="wallet-profile-stats">
                <div>
                  <div className="wallet-stat-value">${formatUsd(summary.positions_value)}</div>
                  <div className="wallet-stat-label">Positions Value</div>
                </div>
                <div>
                  <div className={`wallet-stat-value ${(summary.biggest_win?.realized_pnl ?? 0) >= 0 ? 'up' : 'down'}`}>
                    {summary.biggest_win
                      ? `$${formatUsd(summary.biggest_win.realized_pnl)}`
                      : '—'}
                  </div>
                  <div className="wallet-stat-label">Biggest Win</div>
                </div>
                <div>
                  <div className={`wallet-stat-value ${(summary.total_pnl ?? 0) >= 0 ? 'up' : 'down'}`}>
                    {summary.total_pnl != null ? fmtSignedUsd(summary.total_pnl) : '—'}
                  </div>
                  <div className="wallet-stat-label">All-time PnL</div>
                </div>
              </div>
            </div>

            <div className="wallet-pnl-card">
              <div className="wallet-pnl-header">
                <div className="wallet-pnl-title">
                  Profit/Loss
                  {pnlValue != null && (
                    <span className={`wallet-pnl-tri ${pnlPositive ? 'up' : 'down'}`} aria-hidden>
                      {pnlPositive ? '▲' : '▼'}
                    </span>
                  )}
                </div>
                <div className="wallet-interval-pills" role="group" aria-label="PnL interval">
                  {INTERVALS.map((opt) => (
                    <button
                      key={opt.id}
                      type="button"
                      className={`wallet-interval-pill${interval === opt.id ? ' active' : ''}`}
                      disabled={pnlLoading}
                      onClick={() => setInterval(opt.id)}
                    >
                      {opt.label}
                    </button>
                  ))}
                </div>
              </div>
              <div className={`wallet-pnl-value ${pnlPositive ? 'up' : 'down'}`}>
                {pnlLoading && !pnl ? '…' : fmtSignedUsd(pnlValue)}
              </div>
              <div className="wallet-pnl-sub">{intervalMeta.subtitle}</div>
              <div className="wallet-pnl-chart">
                {chartData.length > 1 ? (
                  <ResponsiveContainer width="100%" height={160}>
                    <ComposedChart data={chartData} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
                      <defs>
                        <linearGradient id="walletPnlFill" x1="0" y1="0" x2="0" y2="1">
                          <stop
                            offset="0%"
                            stopColor={pnlPositive ? 'var(--up)' : 'var(--down)'}
                            stopOpacity={0.28}
                          />
                          <stop
                            offset="100%"
                            stopColor={pnlPositive ? 'var(--up)' : 'var(--down)'}
                            stopOpacity={0.02}
                          />
                        </linearGradient>
                      </defs>
                      <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" vertical={false} />
                      <XAxis
                        dataKey="t"
                        type="number"
                        domain={['dataMin', 'dataMax']}
                        tickFormatter={(v) => fmtChartTick(Number(v), interval)}
                        tick={{ fontSize: 11, fill: 'var(--muted)' }}
                        minTickGap={40}
                      />
                      <YAxis
                        dataKey="delta"
                        width={56}
                        tick={{ fontSize: 11, fill: 'var(--muted)' }}
                        tickFormatter={(v) => fmtSignedUsd(Number(v))}
                      />
                      <Tooltip
                        contentStyle={{
                          background: 'var(--bg-panel)',
                          border: '1px solid var(--border)',
                          borderRadius: 8,
                          fontSize: 12,
                        }}
                        labelFormatter={(v) => new Date(Number(v)).toLocaleString()}
                        formatter={(value) => [fmtSignedUsd(Number(value ?? 0)), 'PnL']}
                      />
                      <Area
                        type="monotone"
                        dataKey="delta"
                        stroke="none"
                        fill="url(#walletPnlFill)"
                        isAnimationActive={false}
                      />
                      <Line
                        type="monotone"
                        dataKey="delta"
                        stroke={pnlPositive ? 'var(--up)' : 'var(--down)'}
                        strokeWidth={2}
                        dot={false}
                        isAnimationActive={false}
                      />
                    </ComposedChart>
                  </ResponsiveContainer>
                ) : (
                  <p className="muted" style={{ margin: '1.5rem 0', textAlign: 'center' }}>
                    {pnlLoading ? 'Loading chart…' : 'No PnL series for this interval'}
                  </p>
                )}
              </div>
            </div>
          </section>
        )}

        {wallet && (
          <section className="wallet-split-row wallet-split-row-3">
            <div className="wallet-panel">
              <div className="wallet-panel-head">
                <h2>PnL by day</h2>
                <span className="muted">{daily.length} days</span>
              </div>
              <div className="wallet-daily-list">
                {daily.length === 0 && (
                  <p className="muted" style={{ padding: '0.75rem' }}>
                    {loading ? 'Loading…' : 'No daily PnL yet'}
                  </p>
                )}
                {daily.map((row) => (
                  <button
                    key={row.date}
                    type="button"
                    className={`wallet-daily-row${date === row.date ? ' active' : ''}`}
                    onClick={() => setDate(row.date)}
                  >
                    <span className="wallet-daily-date">{row.date}</span>
                    <span className={`wallet-daily-pnl ${row.pnl >= 0 ? 'up' : 'down'}`}>
                      {fmtSignedUsd(row.pnl)}
                    </span>
                  </button>
                ))}
              </div>
            </div>

            <div className="wallet-panel">
              <div className="wallet-panel-head">
                <h2>PnL by market · {date}</h2>
                <span className="muted">
                  {activityLoading
                    ? 'Loading…'
                    : `${marketPnls.length} mkts${
                        marketsTotalPnl != null ? ` · ${fmtSignedUsd(marketsTotalPnl)}` : ''
                      }`}
                </span>
              </div>
              <div className="wallet-daily-list">
                {!activityLoading && marketPnls.length === 0 && (
                  <p className="muted" style={{ padding: '0.75rem' }}>
                    No closed-market PnL on this date
                  </p>
                )}
                {marketPnls.map((m) => {
                  const key = m.condition_id || m.slug || m.title || 'm'
                  return (
                    <button
                      key={key}
                      type="button"
                      className={`wallet-market-pnl-row${
                        expandedMarket === key ? ' active' : ''
                      }`}
                      onClick={() => setExpandedMarket(key)}
                      title={m.title || undefined}
                    >
                      <span className="wallet-market-pnl-title">
                        {shortMarketLabel(m.title, m.slug)}
                      </span>
                      <span
                        className={`wallet-daily-pnl ${(m.pnl ?? 0) >= 0 ? 'up' : 'down'}`}
                      >
                        {m.pnl != null ? fmtSignedUsd(m.pnl) : '—'}
                      </span>
                    </button>
                  )
                })}
              </div>
            </div>

            <div className="wallet-panel wallet-activity-panel">
              <div className="wallet-panel-head">
                <h2>Activity</h2>
                <span className="muted">
                  {activityLoading
                    ? '…'
                    : selectedMarketActivity
                      ? `${selectedMarketActivity.n_events} · $${formatCompactUsd(selectedMarketActivity.volume_usd)}`
                      : expandedMarket
                        ? 'None'
                        : '—'}
                </span>
              </div>
              {(selectedMarketMeta?.title ||
                selectedMarketMeta?.slug ||
                selectedMarketActivity?.title ||
                selectedMarketActivity?.slug) && (
                <div className="wallet-activity-selected-title">
                  <span
                    title={
                      selectedMarketMeta?.title ||
                      selectedMarketActivity?.title ||
                      undefined
                    }
                  >
                    {shortMarketLabel(
                      selectedMarketMeta?.title || selectedMarketActivity?.title,
                      selectedMarketMeta?.slug || selectedMarketActivity?.slug,
                    )}
                  </span>
                  {(selectedMarketMeta?.pnl != null || selectedMarketActivity?.pnl != null) && (
                    <span
                      className={`wallet-daily-pnl ${
                        ((selectedMarketMeta?.pnl ?? selectedMarketActivity?.pnl) ?? 0) >= 0
                          ? 'up'
                          : 'down'
                      }`}
                    >
                      {fmtSignedUsd(selectedMarketMeta?.pnl ?? selectedMarketActivity?.pnl)}
                    </span>
                  )}
                </div>
              )}
              {selectedMarketActivity && (boughtShares.up > 0 || boughtShares.down > 0) && (
                <div className="wallet-bought-bar">
                  <span>
                    Bought <span className="up">Up</span>{' '}
                    <strong>{formatCompactUsd(boughtShares.up)}</strong>
                  </span>
                  <span className="wallet-bought-sep">·</span>
                  <span>
                    <span className="down">Down</span>{' '}
                    <strong>{formatCompactUsd(boughtShares.down)}</strong>
                  </span>
                </div>
              )}
              <div className="wallet-fill-list">
                {!activityLoading && !expandedMarket && (
                  <p className="muted wallet-fill-empty">Select a market</p>
                )}
                {!activityLoading && expandedMarket && !selectedMarketActivity && (
                  <p className="muted wallet-fill-empty">No fills</p>
                )}
                {selectedMarketActivity?.activity.map((row, i) => {
                  const side = (row.side || '').toUpperCase()
                  const isBuy = side === 'BUY'
                  const outcome = row.outcome || ''
                  const outcomeUp = outcome.toLowerCase() === 'up'
                  return (
                    <div
                      key={`${row.transaction_hash || 'x'}-${row.timestamp}-${i}`}
                      className="wallet-fill-row"
                    >
                      <span className="wallet-fill-time">{fmtTimeShort(row.timestamp)}</span>
                      <span className="wallet-fill-action">
                        <span className={isBuy ? 'up' : side === 'SELL' ? 'down' : undefined}>
                          {side === 'BUY' ? 'Buy' : side === 'SELL' ? 'Sell' : row.type || '—'}
                        </span>{' '}
                        {outcome && (
                          <span className={outcomeUp ? 'up' : 'down'}>{outcome}</span>
                        )}
                      </span>
                      <span className="wallet-fill-size">
                        {formatCompactUsd(row.shares)}
                        {row.price != null ? (
                          <>
                            {' '}
                            <span className="muted">@</span> {formatCents(row.price)}
                          </>
                        ) : null}
                      </span>
                      <span className="wallet-fill-usd">${formatCompactUsd(row.usd)}</span>
                      <span className="wallet-fill-links">
                        {row.polygonscan_url && (
                          <a
                            href={row.polygonscan_url}
                            target="_blank"
                            rel="noreferrer"
                            title="Polygonscan"
                          >
                            ↗
                          </a>
                        )}
                        {row.orbscan_url && (
                          <a
                            href={row.orbscan_url}
                            target="_blank"
                            rel="noreferrer"
                            title="Orbscan"
                            className="wallet-fill-orb"
                          >
                            ◈
                          </a>
                        )}
                      </span>
                    </div>
                  )
                })}
              </div>
            </div>
          </section>
        )}
      </div>
    </div>
  )
}
