import { useEffect, useMemo, useRef, useState, type CSSProperties } from 'react'
import { formatUsd } from '../api'

export type BinanceBookLevel = {
  range: string
  suffix: string
  qty: number
  approx_price: number
  notional: number
  price_lo?: number | null
  price_hi?: number | null
  lo_usd?: number
  hi_usd?: number | null
}

export type BinanceBookPayload = {
  symbol?: string
  timestamp: number | null
  mode?: string
  note?: string
  best_bid?: number | null
  best_ask?: number | null
  mid?: number | null
  spread?: number | null
  bids: BinanceBookLevel[]
  asks: BinanceBookLevel[]
  ask_qty?: number
  bid_qty?: number
}

type Props = {
  book: BinanceBookPayload | null
  live?: boolean
  symbol?: string
}

type DepthLevel = BinanceBookLevel & { cumQty: number }

/** Visible band rows in collapsed mode (11 bands total). */
const COLLAPSED_ROWS = 9

function formatBtc(qty: number): string {
  if (qty >= 1000) return qty.toFixed(1)
  if (qty >= 100) return qty.toFixed(2)
  if (qty >= 10) return qty.toFixed(3)
  if (qty >= 1) return qty.toFixed(4)
  if (qty <= 0) return '0'
  return qty.toFixed(5)
}

function formatPx(price: number | null | undefined): string {
  if (price == null || !Number.isFinite(price)) return '—'
  return `$${formatUsd(price, 2)}`
}

function formatVol(btc: number): string {
  if (btc >= 100) return `${btc.toFixed(1)} BTC`
  if (btc >= 10) return `${btc.toFixed(2)} BTC`
  return `${btc.toFixed(3)} BTC`
}

/** Asks arrive farthest→nearest; cum from mid upward then reverse for display. */
function withAskCum(asks: BinanceBookLevel[]): DepthLevel[] {
  let running = 0
  const fromMid = [...asks].reverse().map((level) => {
    running += level.qty
    return { ...level, cumQty: running }
  })
  return fromMid.reverse()
}

function withBidCum(bids: BinanceBookLevel[]): DepthLevel[] {
  let running = 0
  return bids.map((level) => {
    running += level.qty
    return { ...level, cumQty: running }
  })
}

function DepthRow({
  level,
  kind,
  maxCum,
  showTag,
}: {
  level: DepthLevel
  kind: 'ask' | 'bid'
  maxCum: number
  showTag?: 'Asks' | 'Bids'
}) {
  const width = maxCum > 0 ? Math.min(100, (level.cumQty / maxCum) * 100) : 0
  return (
    <div className={`ob-row ${kind}`}>
      <div className="ob-cell ob-tag">
        <div
          className="ob-depth"
          style={{ width: `${Math.max(width, level.qty > 0 ? 2 : 0)}%` }}
        />
        {showTag ? <span className={`ob-pill ${kind}`}>{showTag}</span> : null}
      </div>
      <div className={`ob-cell ob-range ${kind}`}>{level.range}</div>
      <div className="ob-cell ob-shares">{formatBtc(level.qty)}</div>
      <div className="ob-cell ob-total">{formatBtc(level.cumQty)}</div>
    </div>
  )
}

function ExpandIcon({ expanded }: { expanded: boolean }) {
  if (expanded) {
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

export default function BinanceOrderBookPanel({
  book,
  live = false,
  symbol = 'BTCUSDT',
}: Props) {
  const [expanded, setExpanded] = useState(false)
  const bodyRef = useRef<HTMLDivElement>(null)
  const midRef = useRef<HTMLDivElement>(null)

  const askLevels = useMemo(() => (book ? withAskCum(book.asks) : []), [book])
  const bidLevels = useMemo(() => (book ? withBidCum(book.bids) : []), [book])

  const maxCum = useMemo(() => {
    const vals = [...askLevels, ...bidLevels].map((l) => l.cumQty)
    return Math.max(1, ...vals, 0)
  }, [askLevels, bidLevels])

  const depthBtc = useMemo(() => {
    if (!book) return 0
    if (book.ask_qty != null || book.bid_qty != null) {
      return (book.ask_qty ?? 0) + (book.bid_qty ?? 0)
    }
    return [...book.asks, ...book.bids].reduce((s, l) => s + l.qty, 0)
  }, [book])

  const hasDepth = Boolean(book && (askLevels.length > 0 || bidLevels.length > 0))

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
  }, [expanded, hasDepth, book?.timestamp])

  return (
    <section className={`ob-panel bn-ob-panel${expanded ? ' ob-expanded' : ''}`}>
      <div className="ob-header">
        <div className="ob-title">
          Binance BTC Order Book
          <span
            className="ob-info"
            title={
              book?.note ||
              'BTC quantity in USD-distance bands from mid (binance_price_orderbook.parquet schema).'
            }
          >
            i
          </span>
        </div>
        <div className="ob-header-right">
          <div className="ob-vol">{formatVol(depthBtc)}</div>
          <button
            type="button"
            className="ob-expand"
            aria-label={expanded ? 'Collapse Binance order book' : 'Expand Binance order book'}
            aria-expanded={expanded}
            title={expanded ? 'Collapse' : 'Expand'}
            onClick={() => setExpanded((v) => !v)}
          >
            <ExpandIcon expanded={expanded} />
          </button>
        </div>
      </div>

      <div className="ob-cols">
        <div className="ob-cell ob-tag">{book?.symbol || symbol}</div>
        <div className="ob-cell">RANGE</div>
        <div className="ob-cell">SIZE</div>
        <div className="ob-cell">TOTAL</div>
      </div>

      {!live && (
        <div className="ob-empty muted">Binance depth is available in live mode only</div>
      )}
      {live && !hasDepth && (
        <div className="ob-empty muted">Waiting for Binance depth…</div>
      )}

      {live && hasDepth && book && (
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
                />
              ))
            )}
          </div>

          <div className="ob-mid" ref={midRef}>
            <span>Mid: {formatPx(book.mid)}</span>
            <span>
              Spread:{' '}
              {book.spread != null && Number.isFinite(book.spread)
                ? `$${formatUsd(book.spread, 2)}`
                : '—'}
            </span>
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
                />
              ))
            )}
          </div>
        </div>
      )}
    </section>
  )
}
