import type { LiveActivityTrade } from '../api'

type Props = {
  enabled?: boolean
  live?: boolean
  trades: LiveActivityTrade[]
  nowMs?: number
  selectedWallet?: string | null
  onSelectWallet?: (wallet: string | null) => void
}

function shortenAddress(value: string): string {
  const s = value.trim()
  if (s.length <= 12) return s
  // 0xabc...def4 — keep prefix + last 4
  if (/^0x[a-fA-F0-9]{12,}$/.test(s)) return `${s.slice(0, 6)}...${s.slice(-4)}`
  // Very long non-handle strings (raw wallets without 0x, etc.)
  if (s.length > 20) return `${s.slice(0, 6)}...${s.slice(-4)}`
  return s
}

function shortName(t: LiveActivityTrade): string {
  const n = (t.name || t.pseudonym || '').trim()
  if (n) return shortenAddress(n)
  const w = t.proxy_wallet || ''
  return shortenAddress(w) || 'Trader'
}

function formatAgo(ts: number, nowMs: number): string {
  const sec = Math.max(0, Math.floor((nowMs - ts) / 1000))
  if (sec < 60) return `${sec}s ago`
  const min = Math.floor(sec / 60)
  if (min < 60) return `${min}m ago`
  const hr = Math.floor(min / 60)
  return `${hr}h ago`
}

function formatCents(price: number): string {
  if (!Number.isFinite(price)) return '—'
  return `${(price * 100).toFixed(1)}¢`
}

function formatUsd(n: number): string {
  if (!Number.isFinite(n)) return '$0'
  if (n >= 1000) return `$${(n / 1000).toFixed(1)}k`
  if (n >= 10) return `$${n.toFixed(0)}`
  return `$${n.toFixed(2)}`
}

function explorerUrl(tx?: string | null): string | null {
  if (!tx) return null
  return `https://polygonscan.com/tx/${tx}`
}

export default function ActivityPanel({
  enabled = false,
  live = false,
  trades,
  nowMs: nowProp,
  selectedWallet = null,
  onSelectWallet,
}: Props) {
  const nowMs = nowProp ?? Date.now()
  const sel = selectedWallet?.toLowerCase() ?? null
  if (!enabled) {
    return (
      <section className="activity-panel">
        <div className="activity-panel-header">
          <div className="activity-panel-title">Activity</div>
        </div>
        <p className="activity-panel-empty">No market selected</p>
      </section>
    )
  }

  return (
    <section className="activity-panel">
      <div className="activity-panel-header">
        <div className="activity-panel-title">Activity</div>
        {live ? (
          <div className="activity-live-badge" aria-label="Live">
            <i /> Live
          </div>
        ) : null}
      </div>
      {trades.length === 0 ? (
        <p className="activity-panel-empty">{live ? 'Waiting for trades…' : 'No trades found'}</p>
      ) : (
        <ul className="activity-tape">
          {trades.map((t) => {
            const up = t.outcome === 'Up'
            const href = explorerUrl(t.transaction_hash)
            const wallet = (t.proxy_wallet || '').toLowerCase()
            const isSelected = sel != null && wallet === sel
            const dimmed = sel != null && !isSelected
            return (
              <li
                key={t.id}
                className={`activity-tape-row${isSelected ? ' selected' : ''}${dimmed ? ' dimmed' : ''}`}
                onClick={() => {
                  if (!onSelectWallet || !wallet) return
                  onSelectWallet(isSelected ? null : wallet)
                }}
                onKeyDown={(e) => {
                  if (!onSelectWallet || !wallet) return
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault()
                    onSelectWallet(isSelected ? null : wallet)
                  }
                }}
                role={onSelectWallet ? 'button' : undefined}
                tabIndex={onSelectWallet && wallet ? 0 : undefined}
              >
                <div className="activity-tape-avatar" aria-hidden>
                  {t.profile_image ? (
                    <img src={t.profile_image} alt="" />
                  ) : (
                    <span>{shortName(t).slice(0, 1).toUpperCase()}</span>
                  )}
                </div>
                <div className="activity-tape-body">
                  <div className="activity-tape-line">
                    <strong className="activity-tape-name">{shortName(t)}</strong>{' '}
                    <span className="activity-tape-action">
                      {(t.side || 'BUY').toLowerCase()}{' '}
                      {Math.round(t.shares).toLocaleString()}{' '}
                      <span className={up ? 'up' : 'down'}>{t.outcome}</span>
                    </span>
                  </div>
                  <div className="activity-tape-meta">
                    at {formatCents(t.price)} ({formatUsd(t.usd)})
                  </div>
                </div>
                <div className="activity-tape-right">
                  <span className="activity-tape-ago">{formatAgo(t.timestamp, nowMs)}</span>
                  {href && (
                    <a
                      className="activity-tape-link"
                      href={href}
                      target="_blank"
                      rel="noreferrer"
                      title="View transaction"
                      onClick={(e) => e.stopPropagation()}
                    >
                      ↗
                    </a>
                  )}
                </div>
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}
