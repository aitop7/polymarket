import { useEffect, useMemo, useState } from 'react'
import {
  api,
  type LiveActivityTrade,
  type MarketTradersResponse,
  type TraderDetailResponse,
  type TraderStatRow,
} from '../api'

type Props = {
  enabled?: boolean
  marketId?: string | null
  data: MarketTradersResponse | null
  selectedWallet?: string | null
  onSelectWallet?: (wallet: string | null) => void
  /** Enrich display names from activity tape */
  activityTrades?: LiveActivityTrade[]
  /** Fills for chart markers (from detail fetch) */
  onDetailChange?: (detail: TraderDetailResponse | null) => void
  /** Playhead filter for detail fill list (ms) */
  playheadTs?: number | null
}

function truncateWallet(wallet: string): string {
  if (!wallet || wallet.length < 12) return wallet || '—'
  return `${wallet.slice(0, 6)}…${wallet.slice(-4)}`
}

function formatUsdCompact(n: number): string {
  if (!Number.isFinite(n)) return '—'
  const abs = Math.abs(n)
  const sign = n < 0 ? '-' : ''
  if (abs >= 1000) return `${sign}$${(abs / 1000).toFixed(1)}k`
  if (abs >= 10) return `${sign}$${abs.toFixed(0)}`
  return `${sign}$${abs.toFixed(2)}`
}

function formatUsd(n: number): string {
  if (!Number.isFinite(n)) return '—'
  return `$${n.toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`
}

function formatCents(price: number): string {
  if (!Number.isFinite(price)) return '—'
  return `${(price * 100).toFixed(1)}¢`
}

function formatShares(n: number): string {
  if (!Number.isFinite(n)) return '—'
  const rounded = Math.round(n * 100) / 100
  return rounded.toLocaleString(undefined, { maximumFractionDigits: 2 })
}

function formatTime(ts: number): string {
  return new Date(ts).toLocaleTimeString(undefined, {
    hour: 'numeric',
    minute: '2-digit',
    second: '2-digit',
  })
}

function nameForWallet(wallet: string, names: Map<string, string>): string {
  const n = names.get(wallet.toLowerCase())
  if (n) return n
  return truncateWallet(wallet)
}

function TraderTable({
  rows,
  selectedWallet,
  onSelectWallet,
  names,
  resolved,
  winner,
}: {
  rows: TraderStatRow[]
  selectedWallet?: string | null
  onSelectWallet?: (wallet: string | null) => void
  names: Map<string, string>
  resolved: boolean
  winner: string | null
}) {
  return (
    <div className="traders-table-wrap">
      <div className="traders-section-head">
        <h3>Top earners</h3>
        <span>{resolved ? `Settled ${winner ?? ''}`.trim() : 'Cash only (open)'}</span>
      </div>
      {rows.length === 0 ? (
        <p className="traders-empty">No traders</p>
      ) : (
        <table className="traders-table">
          <thead>
            <tr>
              <th className="traders-th-rank">#</th>
              <th className="traders-th-name">Trader</th>
              <th className="traders-th-num">Earned</th>
              <th className="traders-th-num">Volume</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row, i) => {
              const w = row.wallet.toLowerCase()
              const selected = selectedWallet != null && selectedWallet.toLowerCase() === w
              const tone = row.pnl >= 0 ? 'up' : 'down'
              return (
                <tr
                  key={w}
                  className={`traders-tr${selected ? ' selected' : ''}`}
                  onClick={() => onSelectWallet?.(selected ? null : w)}
                >
                  <td className="traders-td-rank">{i + 1}</td>
                  <td className="traders-td-name" title={row.wallet}>
                    {nameForWallet(row.wallet, names)}
                  </td>
                  <td className={`traders-td-num traders-metric ${tone}`}>
                    {formatUsdCompact(row.pnl)}
                  </td>
                  <td className="traders-td-num traders-metric neutral">
                    {formatUsdCompact(row.volume_usd)}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
    </div>
  )
}

function TraderDetailView({
  detail,
  name,
  playheadTs,
  onBack,
}: {
  detail: TraderDetailResponse
  name: string
  playheadTs?: number | null
  onBack: () => void
}) {
  const [copied, setCopied] = useState(false)
  const fills = useMemo(() => {
    const list = detail.fills_list ?? []
    if (playheadTs == null) return [...list].reverse()
    return list.filter((f) => f.timestamp <= playheadTs).reverse()
  }, [detail.fills_list, playheadTs])

  const pnlTone = detail.pnl >= 0 ? 'up' : 'down'
  const polygonUrl = `https://polygonscan.com/address/${detail.wallet}`

  const copyWallet = async () => {
    const addr = detail.wallet
    if (!addr) return
    try {
      await navigator.clipboard.writeText(addr)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    } catch {
      // Fallback for older browsers / denied permission
      try {
        const ta = document.createElement('textarea')
        ta.value = addr
        ta.setAttribute('readonly', '')
        ta.style.position = 'fixed'
        ta.style.left = '-9999px'
        document.body.appendChild(ta)
        ta.select()
        document.execCommand('copy')
        document.body.removeChild(ta)
        setCopied(true)
        window.setTimeout(() => setCopied(false), 1500)
      } catch {
        /* ignore */
      }
    }
  }

  return (
    <div className="traders-detail">
      <div className="traders-detail-bar">
        <button type="button" className="traders-clear" onClick={onBack}>
          ← Back
        </button>
        <a
          className="traders-detail-link"
          href={polygonUrl}
          target="_blank"
          rel="noreferrer"
          title="Polygonscan"
        >
          Explorer ↗
        </a>
      </div>
      <div className="traders-detail-identity">
        <div className="traders-detail-name">{name}</div>
        <div className="traders-detail-wallet-row">
          <span className="traders-detail-wallet" title={detail.wallet}>
            {truncateWallet(detail.wallet)}
          </span>
          <button
            type="button"
            className="traders-copy-btn"
            onClick={copyWallet}
            title={copied ? 'Copied' : 'Copy wallet address'}
            aria-label={copied ? 'Copied' : 'Copy wallet address'}
          >
            {copied ? 'Copied' : 'Copy'}
          </button>
        </div>
      </div>
      <div className="traders-detail-stats">
        <div className="traders-detail-stat">
          <span className="traders-detail-label">Earned</span>
          <span className={`traders-metric ${pnlTone}`}>{formatUsd(detail.pnl)}</span>
        </div>
        <div className="traders-detail-stat">
          <span className="traders-detail-label">Volume</span>
          <span className="traders-metric neutral">{formatUsd(detail.volume_usd)}</span>
        </div>
        <div className="traders-detail-stat">
          <span className="traders-detail-label">Fills</span>
          <span className="traders-metric neutral">{detail.fills}</span>
        </div>
      </div>
      <div className="traders-detail-stats">
        <div className="traders-detail-stat">
          <span className="traders-detail-label">Bought</span>
          <span className="traders-metric neutral">
            {formatUsd(detail.buy_usd ?? 0)} ({detail.buy_fills ?? 0})
          </span>
        </div>
        <div className="traders-detail-stat">
          <span className="traders-detail-label">Sold</span>
          <span className="traders-metric neutral">
            {formatUsd(detail.sell_usd ?? 0)} ({detail.sell_fills ?? 0})
          </span>
        </div>
      </div>
      <div className="traders-detail-stats">
        <div className="traders-detail-stat">
          <span className="traders-detail-label">Up pos</span>
          <span className="traders-metric up">{formatShares(detail.up_pos ?? 0)}</span>
        </div>
        <div className="traders-detail-stat">
          <span className="traders-detail-label">Down pos</span>
          <span className="traders-metric down">{formatShares(detail.down_pos ?? 0)}</span>
        </div>
        {detail.resolved && detail.winner ? (
          <div className="traders-detail-stat">
            <span className="traders-detail-label">Winner</span>
            <span className={`traders-metric ${detail.winner === 'Up' ? 'up' : 'down'}`}>
              {detail.winner}
            </span>
          </div>
        ) : null}
      </div>
      <div className="traders-detail-fills-head">Fills</div>
      {fills.length === 0 ? (
        <p className="traders-empty">No fills</p>
      ) : (
        <ul className="traders-fills">
          {fills.map((f, i) => {
            const href = f.transaction_hash
              ? `https://polygonscan.com/tx/${f.transaction_hash}`
              : null
            return (
              <li key={`${f.timestamp}-${i}`} className="traders-fill-row">
                <div className="traders-fill-main">
                  <span className="traders-fill-action">
                    {f.is_buy ? 'buy' : 'sell'} {formatShares(f.shares)}{' '}
                    <span className={f.is_up ? 'up' : 'down'}>{f.is_up ? 'Up' : 'Down'}</span>
                  </span>
                  <span className="traders-fill-meta">
                    at {formatCents(f.price)} ({formatUsdCompact(f.usd)})
                  </span>
                </div>
                <div className="traders-fill-right">
                  <span className="traders-fill-time">{formatTime(f.timestamp)}</span>
                  {href ? (
                    <a
                      className="traders-detail-link"
                      href={href}
                      target="_blank"
                      rel="noreferrer"
                      onClick={(e) => e.stopPropagation()}
                    >
                      ↗
                    </a>
                  ) : null}
                </div>
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}

export default function TradersPanel({
  enabled = true,
  marketId = null,
  data,
  selectedWallet = null,
  onSelectWallet,
  activityTrades = [],
  onDetailChange,
  playheadTs = null,
}: Props) {
  const [detail, setDetail] = useState<TraderDetailResponse | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)

  const names = useMemo(() => {
    const m = new Map<string, string>()
    for (const t of activityTrades) {
      const w = (t.proxy_wallet || '').toLowerCase()
      if (!w || m.has(w)) continue
      const label = (t.name || t.pseudonym || '').trim()
      if (label) m.set(w, label.length > 18 ? `${label.slice(0, 16)}…` : label)
    }
    return m
  }, [activityTrades])

  useEffect(() => {
    if (!enabled || !marketId || !selectedWallet) {
      setDetail(null)
      onDetailChange?.(null)
      return
    }
    let cancelled = false
    setDetailLoading(true)
    api
      .marketTraderDetail(marketId, selectedWallet)
      .then((d) => {
        if (cancelled) return
        setDetail(d)
        onDetailChange?.(d)
      })
      .catch(() => {
        if (cancelled) return
        setDetail(null)
        onDetailChange?.(null)
      })
      .finally(() => {
        if (!cancelled) setDetailLoading(false)
      })
    return () => {
      cancelled = true
    }
    // onDetailChange is setState from parent (stable)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, marketId, selectedWallet])

  if (!enabled) return null

  const showingDetail = Boolean(selectedWallet)

  return (
    <section className="traders-panel traders-panel-grow">
      <div className="traders-panel-header">
        <div className="traders-panel-title">
          {showingDetail ? 'Trader' : 'Top traders'}
        </div>
      </div>
      {!data ? (
        <p className="traders-empty">Loading…</p>
      ) : showingDetail ? (
        detailLoading && !detail ? (
          <p className="traders-empty">Loading…</p>
        ) : detail ? (
          <TraderDetailView
            detail={detail}
            name={nameForWallet(detail.wallet, names)}
            playheadTs={playheadTs}
            onBack={() => onSelectWallet?.(null)}
          />
        ) : (
          <p className="traders-empty">Trader not found</p>
        )
      ) : (
        <div className="traders-scroll traders-scroll-tall">
          <TraderTable
            rows={data.by_pnl}
            selectedWallet={selectedWallet}
            onSelectWallet={onSelectWallet}
            names={names}
            resolved={data.resolved}
            winner={data.winner}
          />
        </div>
      )}
    </section>
  )
}
