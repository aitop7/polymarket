import { useMemo, useState } from 'react'
import { formatUsd } from '../api'

export type BookLevel = {
  range: string
  suffix: string
  shares: number
  approx_price: number
  notional: number
  price_lo?: number
  price_hi?: number | null
}

export type SideBook = {
  traded_price: number
  best_bid: number | null
  best_ask: number | null
  spread: number | null
  asks: BookLevel[]
  bids: BookLevel[]
  ask_shares: number
  bid_shares: number
  volume_shares: number
}

export type BookPayload = {
  timestamp: number | null
  mode?: string
  note?: string
  up: SideBook | null
  down: SideBook | null
}

type Props = {
  book: BookPayload | null
}

function formatAbsRange(level: BookLevel): string {
  if (level.price_lo != null) {
    let lo = Math.round(Math.max(0, Math.min(100, level.price_lo * 100)))
    if (level.price_hi == null) return `${lo}¢+`
    let hi = Math.round(Math.max(0, Math.min(100, level.price_hi * 100)))
    if (lo > hi) [lo, hi] = [hi, lo]
    if (lo === hi) return `${lo}¢`
    return `${lo}–${hi}¢`
  }
  return level.range
}

function cents(price: number | null | undefined): string {
  if (price == null || Number.isNaN(price)) return '—'
  return `${Math.round(price * 100)}¢`
}

function formatVol(usd: number): string {
  if (usd >= 1000) return `$${(usd / 1000).toFixed(1)}K Vol.`
  return `$${usd.toFixed(0)} Vol.`
}

function DepthRow({
  level,
  kind,
  maxShares,
  showTag,
}: {
  level: BookLevel
  kind: 'ask' | 'bid'
  maxShares: number
  showTag?: 'Asks' | 'Bids'
}) {
  const width = maxShares > 0 ? Math.min(100, (level.shares / maxShares) * 100) : 0
  return (
    <div className={`ob-row ${kind}`}>
      <div className="ob-depth" style={{ width: `${width}%` }} />
      <div className="ob-cell ob-tag">
        {showTag ? <span className={`ob-pill ${kind}`}>{showTag}</span> : null}
      </div>
      <div className={`ob-cell ob-range ${kind}`}>{formatAbsRange(level)}</div>
      <div className="ob-cell ob-shares">
        {level.shares.toLocaleString(undefined, { maximumFractionDigits: 2 })}
      </div>
      <div className="ob-cell ob-total">${formatUsd(level.notional, 2)}</div>
    </div>
  )
}

export default function OrderBookPanel({ book }: Props) {
  const [tab, setTab] = useState<'up' | 'down'>('up')
  const side = tab === 'up' ? book?.up : book?.down

  const maxShares = useMemo(() => {
    if (!side) return 1
    const vals = [...side.asks, ...side.bids].map((l) => l.shares)
    return Math.max(1, ...vals)
  }, [side])

  const volUsd = useMemo(() => {
    if (!side) return 0
    return [...side.asks, ...side.bids].reduce((s, l) => s + l.notional, 0)
  }, [side])

  return (
    <section className="ob-panel">
      <div className="ob-header">
        <div className="ob-title">
          Order Book
          <span className="ob-info" title={book?.note || 'Absolute price ranges from distance buckets'}>
            i
          </span>
        </div>
        <div className="ob-vol">{formatVol(volUsd)}</div>
      </div>

      <div className="ob-tabs">
        <button type="button" className={tab === 'up' ? 'active' : ''} onClick={() => setTab('up')}>
          Trade Up
        </button>
        <button type="button" className={tab === 'down' ? 'active' : ''} onClick={() => setTab('down')}>
          Trade Down
        </button>
        <div className="ob-tabs-note muted">Absolute ¢ ranges (from traded price bands)</div>
      </div>

      <div className="ob-cols">
        <div className="ob-cell ob-tag">{tab === 'up' ? 'TRADE UP' : 'TRADE DOWN'}</div>
        <div className="ob-cell">PRICE RANGE</div>
        <div className="ob-cell">SHARES</div>
        <div className="ob-cell">TOTAL</div>
      </div>

      {!side && <div className="ob-empty muted">No order book depth for this market</div>}

      {side && (
        <>
          <div className="ob-section asks">
            {side.asks.map((level, i) => (
              <DepthRow
                key={`a-${level.suffix}`}
                level={level}
                kind="ask"
                maxShares={maxShares}
                showTag={i === side.asks.length - 1 ? 'Asks' : undefined}
              />
            ))}
          </div>

          <div className="ob-mid">
            <span>Last: {cents(side.traded_price)}</span>
            <span>Spread: {side.spread != null ? cents(side.spread) : '—'}</span>
          </div>

          <div className="ob-section bids">
            {side.bids.map((level, i) => (
              <DepthRow
                key={`b-${level.suffix}`}
                level={level}
                kind="bid"
                maxShares={maxShares}
                showTag={i === 0 ? 'Bids' : undefined}
              />
            ))}
          </div>
        </>
      )}
    </section>
  )
}
