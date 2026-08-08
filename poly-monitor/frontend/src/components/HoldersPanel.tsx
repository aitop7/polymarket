import { useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { HolderRow, LiveHoldersResponse } from '../api'

type Props = {
  enabled: boolean
  data: LiveHoldersResponse | null
  revision: number
  onReload?: () => void | Promise<void>
  reloading?: boolean
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

function sortHolders(rows: HolderRow[]): HolderRow[] {
  return [...rows].sort((a, b) => {
    const da = Number(b.amount) - Number(a.amount)
    if (da !== 0) return da
    return String(a.proxy_wallet).localeCompare(String(b.proxy_wallet))
  })
}

function HolderAvatar({ row }: { row: HolderRow }) {
  const [broken, setBroken] = useState(false)
  const hue = avatarHue(row.proxy_wallet || row.display_name)
  const initial = (row.display_name || '?').trim().charAt(0).toUpperCase() || '?'

  if (row.profile_image && !broken) {
    return (
      <span className="holders-avatar">
        <img src={row.profile_image} alt="" onError={() => setBroken(true)} />
      </span>
    )
  }

  return (
    <span
      className="holders-avatar holders-avatar-fallback"
      style={{
        background: `linear-gradient(135deg, hsl(${hue} 70% 52%), hsl(${(hue + 40) % 360} 65% 42%))`,
      }}
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
  revision,
}: {
  title: string
  rows: HolderRow[]
  tone: 'up' | 'down'
  revision: number
}) {
  const listRef = useRef<HTMLUListElement>(null)
  const prevTops = useRef(new Map<string, number>())
  const prevRanks = useRef(new Map<string, number>())
  const sorted = useMemo(() => sortHolders(rows), [rows])

  useLayoutEffect(() => {
    const list = listRef.current
    if (!list) return
    const nodes = list.querySelectorAll<HTMLElement>('[data-wallet]')
    const nextTops = new Map<string, number>()
    const nextRanks = new Map<string, number>()

    nodes.forEach((el, index) => {
      const wallet = el.dataset.wallet
      if (!wallet) return
      const top = el.getBoundingClientRect().top
      nextTops.set(wallet, top)
      nextRanks.set(wallet, index)

      const prevTop = prevTops.current.get(wallet)
      const prevRank = prevRanks.current.get(wallet)
      if (prevTop != null) {
        const dy = prevTop - top
        if (Math.abs(dy) > 0.5) {
          el.style.transition = 'none'
          el.style.transform = `translateY(${dy}px)`
          void el.offsetHeight
          el.style.transition = 'transform 480ms cubic-bezier(0.22, 1, 0.36, 1)'
          el.style.transform = 'translateY(0)'
        }
      }

      el.classList.remove('holders-rank-up', 'holders-rank-down')
      if (prevRank != null && prevRank !== index) {
        el.classList.add(index < prevRank ? 'holders-rank-up' : 'holders-rank-down')
        window.setTimeout(() => {
          el.classList.remove('holders-rank-up', 'holders-rank-down')
        }, 700)
      }
    })

    prevTops.current = nextTops
    prevRanks.current = nextRanks
  }, [sorted, revision])

  return (
    <section className="holders-col">
      <header className="holders-col-head">
        <h3>{title}</h3>
        <span>SHARES</span>
      </header>
      <ul className="holders-list" ref={listRef}>
        {sorted.length === 0 && <li className="holders-empty">No holders yet</li>}
        {sorted.map((row) => (
          <li
            key={`${tone}-${row.proxy_wallet}`}
            data-wallet={row.proxy_wallet}
            className="holders-row"
          >
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

export default function HoldersPanel({ enabled, data, revision, onReload, reloading = false }: Props) {
  if (!enabled) return null

  return (
    <div className="holders-panel">
      <div className="holders-toolbar">
        <div className="holders-live-badge" aria-live="polite">
          <span className="holders-live-dot" />
          Live
        </div>
        {onReload && (
          <button
            type="button"
            className={`holders-reload-btn${reloading ? ' spinning' : ''}`}
            onClick={() => void onReload()}
            disabled={reloading}
            title="Reload holders"
            aria-label="Reload holders"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden>
              <path
                d="M4.5 12a7.5 7.5 0 0 1 12.7-5.4M19.5 12a7.5 7.5 0 0 1-12.7 5.4"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
              />
              <path
                d="M17.2 3.8v4.2h-4.2M6.8 20.2v-4.2h4.2"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </button>
        )}
      </div>
      <div className="holders-grid">
        <HoldersColumn title="Up holders" rows={data?.up ?? []} tone="up" revision={revision} />
        <HoldersColumn title="Down holders" rows={data?.down ?? []} tone="down" revision={revision} />
      </div>
    </div>
  )
}
