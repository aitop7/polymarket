import { useEffect, useState } from 'react'
import { api, type HolderRow, type LiveHoldersResponse } from '../api'

type Props = {
  enabled: boolean
  marketId?: string | null
}

function truncateWallet(wallet: string): string {
  if (!wallet || wallet.length < 12) return wallet || '—'
  return `${wallet.slice(0, 6)}…${wallet.slice(-4)}`
}

function avatarHue(wallet: string): number {
  let h = 0
  for (let i = 0; i < wallet.length; i++) h = (h * 31 + wallet.charCodeAt(i)) >>> 0
  return h % 360
}

function HolderAvatar({ row }: { row: HolderRow }) {
  const [broken, setBroken] = useState(false)
  const hue = avatarHue(row.proxy_wallet || row.display_name)
  const initial = (row.display_name || '?').trim().charAt(0).toUpperCase() || '?'

  if (row.profile_image && !broken) {
    return (
      <span className="holders-avatar">
        <img
          src={row.profile_image}
          alt=""
          onError={() => setBroken(true)}
        />
      </span>
    )
  }

  return (
    <span
      className="holders-avatar holders-avatar-fallback"
      style={{ background: `linear-gradient(135deg, hsl(${hue} 70% 52%), hsl(${(hue + 40) % 360} 65% 42%))` }}
      aria-hidden
    >
      {initial}
    </span>
  )
}

function formatShares(n: number): string {
  if (!Number.isFinite(n)) return '—'
  return n.toLocaleString(undefined, { maximumFractionDigits: 0 })
}

function HoldersColumn({
  title,
  rows,
  tone,
}: {
  title: string
  rows: HolderRow[]
  tone: 'up' | 'down'
}) {
  return (
    <section className="holders-col">
      <header className="holders-col-head">
        <h3>{title}</h3>
        <span>SHARES</span>
      </header>
      <ul className="holders-list">
        {rows.length === 0 && <li className="holders-empty">No holders yet</li>}
        {rows.map((row) => (
          <li key={`${tone}-${row.proxy_wallet}-${row.amount}`} className="holders-row">
            <HolderAvatar row={row} />
            <span className="holders-name" title={row.proxy_wallet}>
              {row.display_name || truncateWallet(row.proxy_wallet)}
            </span>
            <span className={`holders-shares ${tone}`}>{formatShares(row.amount)}</span>
          </li>
        ))}
      </ul>
    </section>
  )
}

export default function HoldersPanel({ enabled, marketId }: Props) {
  const [data, setData] = useState<LiveHoldersResponse | null>(null)

  useEffect(() => {
    if (!enabled) {
      setData(null)
      return
    }
    let cancelled = false
    const load = () => {
      api
        .liveHolders(20)
        .then((res) => {
          if (cancelled) return
          if (marketId && res.market_id && String(res.market_id) !== String(marketId)) return
          setData(res)
        })
        .catch(() => {
          /* ignore transient errors */
        })
    }
    load()
    const id = window.setInterval(load, 3000)
    return () => {
      cancelled = true
      window.clearInterval(id)
    }
  }, [enabled, marketId])

  if (!enabled) return null

  return (
    <div className="holders-panel">
      <div className="holders-live-badge" aria-live="polite">
        <span className="holders-live-dot" />
        Live
      </div>
      <div className="holders-grid">
        <HoldersColumn title="Up holders" rows={data?.up ?? []} tone="up" />
        <HoldersColumn title="Down holders" rows={data?.down ?? []} tone="down" />
      </div>
    </div>
  )
}
