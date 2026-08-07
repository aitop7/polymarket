import { useMemo, useState } from 'react'
import { formatCents, formatCentsInt, formatUsd } from '../api'

export type BookLevel = {
  range: string
  suffix: string
  shares: number
  approx_price: number
  notional: number
  price?: number
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

type DepthLevel = BookLevel & { cumShares: number }

function formatAbsRange(level: BookLevel, ladder: boolean): string {
  if (ladder && level.price != null) {
    return formatCentsInt(level.price)
  }
  if (level.price_lo != null) {
    const lo = Math.round(Math.max(0, Math.min(100, level.price_lo * 100)))
    if (level.price_hi == null) return `${lo}¢+`
    let hi = Math.round(Math.max(0, Math.min(100, level.price_hi * 100)))
    let a = lo
    let b = hi
    if (a > b) [a, b] = [b, a]
    if (a === b) return `${a}¢`
    return ladder ? `${a}¢` : `${a}–${b}¢`
  }
  return level.range
}

function cents(price: number | null | undefined): string {
  return formatCents(price)
}

function formatVol(usd: number): string {
  if (usd >= 1000) return `$${(usd / 1000).toFixed(1)}K Vol.`
  return `$${usd.toFixed(0)} Vol.`
}

function withAskCum(asks: BookLevel[]): DepthLevel[] {
  const visible = asks.filter((l) => l.shares > 0)
  let running = 0
  const fromMid = [...visible].reverse().map((level) => {
    running += level.shares
    return { ...level, cumShares: running }
  })
  return fromMid.reverse()
}

function withBidCum(bids: BookLevel[]): DepthLevel[] {
  const visible = bids.filter((l) => l.shares > 0)
  let running = 0
  return visible.map((level) => {
    running += level.shares
    return { ...level, cumShares: running }
  })
}

function DepthRow({
  level,
  kind,
  maxCum,
  showTag,
  ladder,
}: {
  level: DepthLevel
  kind: 'ask' | 'bid'
  maxCum: number
  showTag?: 'Asks' | 'Bids'
  ladder: boolean
}) {
  const width = maxCum > 0 ? Math.min(100, (level.cumShares / maxCum) * 100) : 0
  return (
    <div className={`ob-row ${kind}`}>
      <div className="ob-depth" style={{ width: `${Math.max(width, level.shares > 0 ? 2 : 0)}%` }} />
      <div className="ob-cell ob-tag">
        {showTag ? <span className={`ob-pill ${kind}`}>{showTag}</span> : null}
      </div>
      <div className={`ob-cell ob-range ${kind}`}>{formatAbsRange(level, ladder)}</div>
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
  const ladder = book?.mode === 'ladder'

  const askLevels = useMemo(() => (side ? withAskCum(side.asks) : []), [side])
  const bidLevels = useMemo(() => (side ? withBidCum(side.bids) : []), [side])

  const maxCum = useMemo(() => {
    const vals = [...askLevels, ...bidLevels].map((l) => l.cumShares)
    return Math.max(1, ...vals, 0)
  }, [askLevels, bidLevels])

  const volUsd = useMemo(() => {
    if (!side) return 0
    return [...side.asks, ...side.bids].reduce((s, l) => s + l.notional, 0)
  }, [side])

  return (
    <section className="ob-panel">
      <div className="ob-header">
        <div className="ob-title">
          Order Book
          <span
            className="ob-info"
            title={
              book?.note ||
              (ladder ? 'Live CLOB price ladder' : 'Absolute price ranges from distance buckets')
            }
          >
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
      </div>

      <div className="ob-cols">
        <div className="ob-cell ob-tag">{tab === 'up' ? 'TRADE UP' : 'TRADE DOWN'}</div>
        <div className="ob-cell">PRICE</div>
        <div className="ob-cell">SHARES</div>
        <div className="ob-cell">TOTAL</div>
      </div>

      {!side && <div className="ob-empty muted">No order book depth for this market</div>}

      {side && askLevels.length === 0 && bidLevels.length === 0 && (
        <div className="ob-empty muted">No depth at this price</div>
      )}

      {side && (askLevels.length > 0 || bidLevels.length > 0) && (
        <>
          <div className="ob-section asks">
            {askLevels.map((level, i) => (
              <DepthRow
                key={`a-${level.suffix}`}
                level={level}
                kind="ask"
                maxCum={maxCum}
                showTag={i === askLevels.length - 1 ? 'Asks' : undefined}
                ladder={ladder}
              />
            ))}
          </div>

          <div className="ob-mid">
            <span>Last: {cents(side.traded_price)}</span>
            <span>Spread: {side.spread != null ? cents(side.spread) : '—'}</span>
          </div>

          <div className="ob-section bids">
            {bidLevels.map((level, i) => (
              <DepthRow
                key={`b-${level.suffix}`}
                level={level}
                kind="bid"
                maxCum={maxCum}
                showTag={i === 0 ? 'Bids' : undefined}
                ladder={ladder}
              />
            ))}
          </div>
        </>
      )}
    </section>
  )
}
