import { useEffect, useMemo, useRef, useState, type CSSProperties } from 'react'
import { formatCentsTrade, formatCentsTradeNumber, formatUsd } from '../api'

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

/** Visible order rows in collapsed (scrollable) mode — about 9 levels. */
const COLLAPSED_ROWS = 9

function formatAbsRange(level: BookLevel, ladder: boolean): string {
  if (ladder && level.price != null) {
    return formatCentsTrade(level.price)
  }
  if (level.price_lo != null) {
    const lo = formatCentsTradeNumber(level.price_lo)
    if (level.price_hi == null) return `${lo}¢+`
    const hi = formatCentsTradeNumber(level.price_hi)
    if (lo === hi) return `${lo}¢`
    return ladder ? `${lo}¢` : `${lo}–${hi}¢`
  }
  return level.range
}

function cents(price: number | null | undefined): string {
  return formatCentsTrade(price)
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
      <div className="ob-cell ob-tag">
        <div
          className="ob-depth"
          style={{ width: `${Math.max(width, level.shares > 0 ? 2 : 0)}%` }}
        />
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

function ExpandIcon({ expanded }: { expanded: boolean }) {
  if (expanded) {
    // Collapse: inward corners
    return (
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <path
          d="M9 3v6H3M15 3v6h6M9 21v-6H3M15 21v-6h6"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    )
  }
  // Expand: outward corners
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M9 3H3v6M15 3h6v6M9 21H3v-6M15 21h6v-6"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}

export default function OrderBookPanel({ book }: Props) {
  const [tab, setTab] = useState<'up' | 'down'>('up')
  const [expanded, setExpanded] = useState(false)
  const bodyRef = useRef<HTMLDivElement>(null)
  const midRef = useRef<HTMLDivElement>(null)
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

  const hasDepth = Boolean(side)

  // Keep the spread / last-price row centered when collapsed.
  useEffect(() => {
    if (expanded || !hasDepth) return
    const body = bodyRef.current
    const mid = midRef.current
    if (!body || !mid) return
    const frame = requestAnimationFrame(() => {
      const top = mid.offsetTop - body.clientHeight / 2 + mid.offsetHeight / 2
      body.scrollTop = Math.max(0, top)
    })
    return () => cancelAnimationFrame(frame)
  }, [tab, expanded, hasDepth])

  return (
    <section className={`ob-panel${expanded ? ' ob-expanded' : ''}`}>
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
        <div className="ob-header-right">
          <div className="ob-vol">{formatVol(volUsd)}</div>
          <button
            type="button"
            className="ob-expand"
            aria-label={expanded ? 'Collapse order book' : 'Expand order book'}
            aria-expanded={expanded}
            title={expanded ? 'Collapse' : 'Expand'}
            onClick={() => setExpanded((v) => !v)}
          >
            <ExpandIcon expanded={expanded} />
          </button>
        </div>
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

      {side && (
        <div
          ref={bodyRef}
          className={`ob-body${expanded ? ' expanded' : ''}`}
          style={
            expanded
              ? undefined
              : ({ ['--ob-visible-rows' as string]: COLLAPSED_ROWS } as CSSProperties)
          }
        >
          <div className="ob-section asks">
            {askLevels.length === 0 ? (
              <div className="ob-empty-side" aria-label="No asks">
                <span className="ob-empty-side-label">No asks</span>
              </div>
            ) : (
              askLevels.map((level, i) => (
                <DepthRow
                  key={`a-${level.suffix}`}
                  level={level}
                  kind="ask"
                  maxCum={maxCum}
                  showTag={i === askLevels.length - 1 ? 'Asks' : undefined}
                  ladder={ladder}
                />
              ))
            )}
          </div>

          <div className="ob-mid" ref={midRef}>
            <span>Last: {cents(side.traded_price)}</span>
            <span>Spread: {side.spread != null ? cents(side.spread) : '—'}</span>
          </div>

          <div className="ob-section bids">
            {bidLevels.length === 0 ? (
              <div className="ob-empty-side" aria-label="No bids">
                <span className="ob-empty-side-label">No bids</span>
              </div>
            ) : (
              bidLevels.map((level, i) => (
                <DepthRow
                  key={`b-${level.suffix}`}
                  level={level}
                  kind="bid"
                  maxCum={maxCum}
                  showTag={i === 0 ? 'Bids' : undefined}
                  ladder={ladder}
                />
              ))
            )}
          </div>
        </div>
      )}
    </section>
  )
}
