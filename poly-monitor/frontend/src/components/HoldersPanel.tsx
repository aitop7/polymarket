import { useEffect, useMemo, useRef, useState } from 'react'
import type { HolderRow, LiveHoldersResponse } from '../api'

type Props = {
  enabled: boolean
  live?: boolean
  data: LiveHoldersResponse | null
  revision?: number
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

/** Whole shares as integers; one decimal only when needed (e.g. 12.5). */
function formatShares(n: number): string {
  if (!Number.isFinite(n)) return '—'
  const rounded = Math.round(n * 10) / 10
  const hasFrac = Math.abs(rounded - Math.round(rounded)) > 1e-9
  return rounded.toLocaleString(undefined, {
    minimumFractionDigits: hasFrac ? 1 : 0,
    maximumFractionDigits: hasFrac ? 1 : 0,
  })
}

function formatDelta(n: number): string {
  if (!Number.isFinite(n) || Math.abs(n) < 1e-9) return ''
  const sign = n > 0 ? '+' : ''
  return `${sign}${formatShares(n)}`
}

type HolderFlash = {
  dir: 'up' | 'down'
  token: number
  delta: number
}

const FLASH_MS = 1500

function HoldersColumn({
  title,
  rows,
  tone,
}: {
  title: string
  rows: HolderRow[]
  tone: 'up' | 'down'
}) {
  const sorted = useMemo(() => sortHolders(rows), [rows])
  const prevAmounts = useRef(new Map<string, number>())
  const flashTimers = useRef(new Map<string, number>())
  const [flashes, setFlashes] = useState(() => new Map<string, HolderFlash>())

  useEffect(() => {
    const prev = prevAmounts.current
    const seen = new Set<string>()
    const deltas: { k: string; dir: 'up' | 'down'; delta: number }[] = []

    for (const row of sorted) {
      const k = row.proxy_wallet.toLowerCase()
      seen.add(k)
      const amount = Number(row.amount) || 0
      const before = prev.get(k)
      if (before == null) {
        if (prev.size > 0 && amount > 0) {
          deltas.push({ k, dir: 'up', delta: amount })
        }
      } else if (Math.abs(before - amount) > 1e-6) {
        const delta = amount - before
        deltas.push({ k, dir: delta > 0 ? 'up' : 'down', delta })
      }
      prev.set(k, amount)
    }

    for (const k of [...prev.keys()]) {
      if (!seen.has(k)) prev.delete(k)
    }

    if (!deltas.length) return
    setFlashes((prevFlashes) => {
      const next = new Map(prevFlashes)
      for (const { k, dir, delta } of deltas) {
        const token = (next.get(k)?.token ?? 0) + 1
        next.set(k, { dir, token, delta })
        const oldTimer = flashTimers.current.get(k)
        if (oldTimer != null) window.clearTimeout(oldTimer)
        const timer = window.setTimeout(() => {
          setFlashes((cur) => {
            const n = new Map(cur)
            const f = n.get(k)
            if (f && f.token === token) n.delete(k)
            return n
          })
          flashTimers.current.delete(k)
        }, FLASH_MS)
        flashTimers.current.set(k, timer)
      }
      return next
    })
  }, [sorted])

  useEffect(() => {
    return () => {
      for (const t of flashTimers.current.values()) window.clearTimeout(t)
      flashTimers.current.clear()
    }
  }, [])

  return (
    <section className="holders-col">
      <header className="holders-col-head">
        <h3>{title}</h3>
        <span>SHARES</span>
      </header>
      <ul className="holders-list">
        {sorted.length === 0 && <li className="holders-empty">No holders yet</li>}
        {sorted.map((row) => {
          const flash = flashes.get(row.proxy_wallet.toLowerCase())
          return (
            <li
              key={
                flash
                  ? `${tone}-${row.proxy_wallet}-${flash.token}`
                  : `${tone}-${row.proxy_wallet}`
              }
              className={`holders-row${flash ? ` holders-flash-${flash.dir}` : ''}`}
            >
              <HolderAvatar row={row} />
              <span className="holders-name" title={row.proxy_wallet}>
                {row.display_name || truncateWallet(row.proxy_wallet)}
              </span>
              <span className="holders-shares-wrap">
                <span className={`holders-shares ${tone}`}>{formatShares(row.amount)}</span>
                {flash && (
                  <span key={flash.token} className={`holders-delta ${flash.dir}`}>
                    {formatDelta(flash.delta)}
                  </span>
                )}
              </span>
            </li>
          )
        })}
      </ul>
    </section>
  )
}

export default function HoldersPanel({ enabled, live = false, data }: Props) {
  if (!enabled) return null

  return (
    <div className="holders-panel">
      <div className="holders-panel-header">
        <div className="holders-panel-title">Top holders</div>
        {live ? (
          <div className="holders-live-badge" aria-live="polite">
            <span className="holders-live-dot" />
            Live
          </div>
        ) : null}
      </div>
      <div className="holders-scroll">
        <div className="holders-grid">
          <HoldersColumn title="Up holders" rows={data?.up ?? []} tone="up" />
          <HoldersColumn title="Down holders" rows={data?.down ?? []} tone="down" />
        </div>
      </div>
    </div>
  )
}
