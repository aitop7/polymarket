import { useEffect, useMemo, useState } from 'react'
import { createPortal } from 'react-dom'
import ChartCollapseButton from './ChartCollapseButton'
import ChartEnlargeButton from './ChartEnlargeButton'
import {
  Bar,
  CartesianGrid,
  ComposedChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import type { TimeDomain } from './PriceChart'

export type VolumePoint = {
  t: number
  bn_buy?: number | null
  bn_sell?: number | null
  up_buy_vol?: number | null
  up_sell_vol?: number | null
  down_buy_vol?: number | null
  down_sell_vol?: number | null
}

type Props = {
  data: VolumePoint[]
  mode: 'binance' | 'outcomes'
  title?: string
  xDomain: TimeDomain
  hoverTime?: number | null
  onHoverTimeChange?: (t: number | null) => void
  /** Live market: wait for several real bars; forming 5s bar grows in height as trades land. */
  live?: boolean
  /** Wall clock (ms) — pins the forming live volume bar to "now". */
  nowMs?: number
  /** Rendered inside the large-chart lightbox (taller plot, close control). */
  lightbox?: boolean
  onLightboxClose?: () => void
}

const Y_AXIS_WIDTH = 72
const CHART_MARGIN = { top: 4, right: 8, left: 4, bottom: 28 }
const VOLUME_BUCKET_MS = 5_000
/** Don't paint live volume until this many real buckets exist. */
const LIVE_MIN_BARS = 3
/** Approx plot width used to size bars from visible time span. */
const PLOT_WIDTH_PX = 720

function formatTimeTick(ms: number): string {
  try {
    return new Intl.DateTimeFormat('en-US', {
      timeZone: 'America/New_York',
      hour: 'numeric',
      minute: '2-digit',
      second: '2-digit',
      hour12: true,
    }).format(new Date(ms))
  } catch {
    return ''
  }
}

function formatVol(v: number, mode: 'binance' | 'outcomes'): string {
  if (!Number.isFinite(v) || v === 0) return '0'
  if (mode === 'binance') {
    if (v >= 1) return v.toFixed(3)
    if (v >= 0.01) return v.toFixed(4)
    return v.toExponential(2)
  }
  if (v >= 1000) return `${(v / 1000).toFixed(1)}k`
  if (v >= 10) return v.toFixed(0)
  return v.toFixed(1)
}

function buySellPair(
  buy: number,
  sell: number,
  pxPerUnit: number,
  bx: number,
  w: number,
  /** Pixel y of the axis edge; bars grow away from axis in `dir`. */
  yAxis: number,
  dir: 'up' | 'down',
  keyPrefix: string,
  /** Inset from axis so above/below bars read as split (white gap). */
  axisGap = 0,
) {
  if (buy <= 0 && sell <= 0) return null
  const buyH = buy * pxPerUnit
  const sellH = sell * pxPerUnit
  const base = dir === 'up' ? yAxis - axisGap : yAxis + axisGap
  const makeRect = (kind: 'buy' | 'sell', h: number, opacity: number) => {
    if (h <= 0) return null
    const height = Math.max(1, h)
    const y = dir === 'up' ? base - height : base
    return (
      <rect
        key={`${keyPrefix}-${kind}-${opacity}`}
        x={bx}
        y={y}
        width={w}
        height={height}
        fill={kind === 'buy' ? '#10b981' : '#ef4444'}
        fillOpacity={opacity}
        stroke="none"
      />
    )
  }
  // Smaller under at 100%; larger on top at 70%.
  const buyOnTop = buy >= sell
  return buyOnTop ? (
    <>
      {sell > 0 && makeRect('sell', sellH, 1)}
      {buy > 0 && makeRect('buy', buyH, 0.7)}
    </>
  ) : (
    <>
      {buy > 0 && makeRect('buy', buyH, 1)}
      {sell > 0 && makeRect('sell', sellH, 0.7)}
    </>
  )
}

/** Draw at our time-domain barSize; center on Recharts' slot (ignore its fluctuating band width). */
function barDrawBox(x: number, width: number, barSize: number): { bx: number; w: number } {
  const w = Math.max(2, barSize)
  const bx = x + (width > 0 ? width / 2 : 0) - w / 2
  return { bx, w }
}

/** Buy + sell both above zero at the same x; smaller bar under, larger on top. */
function BinanceBuySellShape(props: {
  x?: number
  y?: number
  width?: number
  height?: number
  payload?: { buyTip?: number; sellTip?: number; range?: number }
  barSize: number
}) {
  const { x = 0, y = 0, width = 0, height = 0, payload, barSize } = props
  const buy = Number(payload?.buyTip) || 0
  const sell = Number(payload?.sellTip) || 0
  const R = Math.max(Number(payload?.range) || 0, buy, sell, 1e-12)
  if (height <= 0 || (buy <= 0 && sell <= 0)) return null

  // dataKey=range bar spans 0 → range; bottom edge is the zero axis.
  const y0 = y + height
  const pxPerUnit = height / R
  const { bx, w } = barDrawBox(x, width, barSize)

  return (
    <g>{buySellPair(buy, sell, pxPerUnit, bx, w, y0, 'up', 'bn')}</g>
  )
}

/**
 * Up buy/sell above axis; Down buy/sell below. Same green/red overlay as Binance.
 * Placeholder Bar spans 0 → range; scale matches the positive half of a symmetric Y domain.
 */
function OutcomesBuySellShape(props: {
  x?: number
  y?: number
  width?: number
  height?: number
  payload?: {
    upBuyTip?: number
    upSellTip?: number
    downBuyTip?: number
    downSellTip?: number
    range?: number
  }
  barSize: number
}) {
  const { x = 0, y = 0, width = 0, height = 0, payload, barSize } = props
  const upBuy = Number(payload?.upBuyTip) || 0
  const upSell = Number(payload?.upSellTip) || 0
  const downBuy = Number(payload?.downBuyTip) || 0
  const downSell = Number(payload?.downSellTip) || 0
  const R = Math.max(
    Number(payload?.range) || 0,
    upBuy,
    upSell,
    downBuy,
    downSell,
    1e-12,
  )
  if (
    height <= 0 ||
    (upBuy <= 0 && upSell <= 0 && downBuy <= 0 && downSell <= 0)
  ) {
    return null
  }

  // Positive placeholder: bottom edge is y=0. Draw Down into the negative half.
  const y0 = y + height
  const pxPerUnit = height / R
  const { bx, w } = barDrawBox(x, width, barSize)
  // Thin grey band on the zero axis so Up (above) / Down (below) read as split.
  const axisGap = 1

  return (
    <g>
      {buySellPair(upBuy, upSell, pxPerUnit, bx, w, y0, 'up', 'up', axisGap)}
      {buySellPair(downBuy, downSell, pxPerUnit, bx, w, y0, 'down', 'dn', axisGap)}
      <rect x={bx} y={y0 - 0.5} width={w} height={1} fill="#d1d5db" stroke="none" />
    </g>
  )
}

export default function VolumeChart(props: Props) {
  const {
    data,
    mode,
    title,
    xDomain,
    hoverTime,
    onHoverTimeChange,
    live = false,
    lightbox = false,
    onLightboxClose,
  } = props
  const [collapsed, setCollapsed] = useState(false)
  const [enlarged, setEnlarged] = useState(false)

  useEffect(() => {
    if (!lightbox && !enlarged) return
    const onKey = (ev: KeyboardEvent) => {
      if (ev.key !== 'Escape') return
      if (lightbox) onLightboxClose?.()
      else setEnlarged(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [lightbox, enlarged, onLightboxClose])

  // Wider when zoomed in (fewer 5s slots visible); thinner when zoomed out.
  // ~92% of slot width so neighboring 5s bars sit close with a thin gap.
  const barSize = useMemo(() => {
    const span = Math.max(1, xDomain[1] - xDomain[0])
    const slots = Math.max(1, span / VOLUME_BUCKET_MS)
    return Math.round(Math.min(56, Math.max(10, (PLOT_WIDTH_PX / slots) * 0.92)))
  }, [xDomain])

  // Backend attaches one 5s volume sample per bucket; drop empty slots for clearer bars.
  // Live: fold samples into fixed 5s slots so the current bar stays put and grows in height.
  const chartData = useMemo(() => {
    type Row = {
      t: number
      range: number
      buyTip?: number
      sellTip?: number
      upBuyTip?: number
      upSellTip?: number
      downBuyTip?: number
      downSellTip?: number
      _has: boolean
    }

    const toRow = (d: Omit<VolumePoint, 't'>, t: number): Row => {
      if (mode === 'binance') {
        const buy = Number(d.bn_buy) || 0
        const sell = Number(d.bn_sell) || 0
        return {
          t,
          range: Math.max(buy, sell, 1e-9),
          buyTip: buy,
          sellTip: sell,
          _has: buy > 0 || sell > 0,
        }
      }
      const upBuy = Number(d.up_buy_vol) || 0
      const upSell = Number(d.up_sell_vol) || 0
      const downBuy = Number(d.down_buy_vol) || 0
      const downSell = Number(d.down_sell_vol) || 0
      return {
        t,
        range: Math.max(upBuy, upSell, downBuy, downSell, 1e-9),
        upBuyTip: upBuy,
        upSellTip: upSell,
        downBuyTip: downBuy,
        downSellTip: downSell,
        _has: upBuy + upSell + downBuy + downSell > 0,
      }
    }

    if (!live) {
      const rows = data.map((d) => toRow(d, d.t))
      const nonempty = rows.filter((r) => r._has)
      return nonempty.length ? nonempty : rows
    }

    // Aggregate by bucket start; volume is usually on one series point per bucket (use max).
    const byStart = new Map<
      number,
      {
        bn_buy: number
        bn_sell: number
        up_buy_vol: number
        up_sell_vol: number
        down_buy_vol: number
        down_sell_vol: number
      }
    >()
    for (const d of data) {
      const t = Math.floor(Number(d.t))
      if (!Number.isFinite(t)) continue
      // Match backend: a point exactly on a bucket boundary belongs to the previous bucket.
      let start = Math.floor(t / VOLUME_BUCKET_MS) * VOLUME_BUCKET_MS
      if (t === start && start > 0) start -= VOLUME_BUCKET_MS
      const cur = byStart.get(start) ?? {
        bn_buy: 0,
        bn_sell: 0,
        up_buy_vol: 0,
        up_sell_vol: 0,
        down_buy_vol: 0,
        down_sell_vol: 0,
      }
      cur.bn_buy = Math.max(cur.bn_buy, Number(d.bn_buy) || 0)
      cur.bn_sell = Math.max(cur.bn_sell, Number(d.bn_sell) || 0)
      cur.up_buy_vol = Math.max(cur.up_buy_vol, Number(d.up_buy_vol) || 0)
      cur.up_sell_vol = Math.max(cur.up_sell_vol, Number(d.up_sell_vol) || 0)
      cur.down_buy_vol = Math.max(cur.down_buy_vol, Number(d.down_buy_vol) || 0)
      cur.down_sell_vol = Math.max(cur.down_sell_vol, Number(d.down_sell_vol) || 0)
      byStart.set(start, cur)
    }

    // Plot at bucket start (always <= now once the bucket has begun) with fixed 5s spacing.
    const rows: Row[] = []
    for (const [start, vols] of [...byStart.entries()].sort((a, b) => a[0] - b[0])) {
      rows.push(toRow(vols, start))
    }
    const nonempty = rows.filter((r) => r._has)
    // Ignore sparse early volume until several real buckets exist.
    if (nonempty.length < LIVE_MIN_BARS) return []
    return nonempty
  }, [data, mode, live])

  // Only bars in the visible time window — keeps Y scale from off-screen tall bars.
  const visibleData = useMemo(() => {
    const [x0, x1] = xDomain
    return chartData.filter((d) => {
      const t = Number(d.t)
      return Number.isFinite(t) && t >= x0 && t <= x1
    })
  }, [chartData, xDomain])

  const yDomain = useMemo((): [number, number] => {
    let hi = 0
    for (const d of visibleData) {
      if (mode === 'binance') {
        hi = Math.max(hi, Number(d.buyTip) || 0, Number(d.sellTip) || 0)
      } else {
        hi = Math.max(
          hi,
          Number(d.upBuyTip) || 0,
          Number(d.upSellTip) || 0,
          Number(d.downBuyTip) || 0,
          Number(d.downSellTip) || 0,
        )
      }
    }
    if (hi === 0) {
      return mode === 'binance' ? [0, 0.01] : [-10, 10]
    }
    const pad = Math.max(hi * 0.12, mode === 'binance' ? 0.001 : 1)
    if (mode === 'binance') return [0, hi + pad]
    // Symmetric so Up (above) and Down (below) share the same scale.
    return [-(hi + pad), hi + pad]
  }, [visibleData, mode])

  const timeTicks = useMemo(() => {
    const [a, b] = xDomain
    const span = Math.max(1, b - a)
    const n = 5
    return Array.from({ length: n }, (_, i) => a + (span * i) / (n - 1))
  }, [xDomain])

  const tip = useMemo(() => {
    if (!visibleData.length) return null
    const target =
      hoverTime ??
      [...visibleData].reverse().find((d) => d._has)?.t ??
      visibleData[visibleData.length - 1].t
    let best = visibleData[0]
    let bestDist = Math.abs(best.t - target)
    for (const p of visibleData) {
      const dist = Math.abs(p.t - target)
      if (dist < bestDist) {
        best = p
        bestDist = dist
      }
    }
    return best
  }, [visibleData, hoverTime])

  const hasAny = visibleData.some((d) =>
    mode === 'binance'
      ? (Number(d.buyTip) || 0) > 0 || (Number(d.sellTip) || 0) > 0
      : (Number(d.upBuyTip) || 0) +
          (Number(d.upSellTip) || 0) +
          (Number(d.downBuyTip) || 0) +
          (Number(d.downSellTip) || 0) >
        0,
  )

  const binanceShape = useMemo(() => {
    const Shape = (props: {
      x?: number
      y?: number
      width?: number
      height?: number
      payload?: { buyTip?: number; sellTip?: number; range?: number }
    }) => <BinanceBuySellShape {...props} barSize={barSize} />
    return Shape
  }, [barSize])

  const outcomesShape = useMemo(() => {
    const Shape = (props: {
      x?: number
      y?: number
      width?: number
      height?: number
      payload?: {
        upBuyTip?: number
        upSellTip?: number
        downBuyTip?: number
        downSellTip?: number
        range?: number
      }
    }) => <OutcomesBuySellShape {...props} barSize={barSize} />
    return Shape
  }, [barSize])

  const chartLabel =
    title || (mode === 'binance' ? 'Binance BTC volume' : 'Up / Down volume')

  return (
    <>
    <div
      className={`chart-block chart-block-volume${
        collapsed && !lightbox ? ' chart-block-collapsed' : ''
      }${lightbox ? ' chart-block-lightbox' : ''}`}
    >
      <div className="chart-header chart-header-volume">
        <div className="chart-header-left">
          <div className="chart-title-row">
            {!lightbox ? (
              <ChartCollapseButton
                collapsed={collapsed}
                onToggle={() => setCollapsed((v) => !v)}
                label={chartLabel}
              />
            ) : null}
            {title && <div className="chart-title">{title}</div>}
            {!collapsed || lightbox ? (
              <ChartEnlargeButton
                label={chartLabel}
                mode={lightbox ? 'close' : 'enlarge'}
                onClick={() => {
                  if (lightbox) onLightboxClose?.()
                  else setEnlarged(true)
                }}
              />
            ) : null}
          </div>
          {(!collapsed || lightbox) && (
            <div className="chart-header-tip chart-volume-tip">
              {tip ? (
                mode === 'binance' ? (
                  <span className="chart-header-tip-prices">
                    <span className="chart-header-tip-price" style={{ color: '#10b981' }}>
                      Buy {formatVol(Number(tip.buyTip) || 0, 'binance')} BTC
                    </span>
                    <span className="chart-header-tip-price" style={{ color: '#ef4444' }}>
                      Sell {formatVol(Number(tip.sellTip) || 0, 'binance')} BTC
                    </span>
                  </span>
                ) : (
                  <span className="chart-header-tip-prices">
                    <span className="chart-header-tip-price">
                      Up{' '}
                      <span style={{ color: '#10b981' }}>
                        buy {formatVol(Number(tip.upBuyTip) || 0, 'outcomes')}
                      </span>
                      {' / '}
                      <span style={{ color: '#ef4444' }}>
                        sell {formatVol(Number(tip.upSellTip) || 0, 'outcomes')}
                      </span>
                    </span>
                    <span className="chart-header-tip-price">
                      Down{' '}
                      <span style={{ color: '#10b981' }}>
                        buy {formatVol(Number(tip.downBuyTip) || 0, 'outcomes')}
                      </span>
                      {' / '}
                      <span style={{ color: '#ef4444' }}>
                        sell {formatVol(Number(tip.downSellTip) || 0, 'outcomes')}
                      </span>
                    </span>
                  </span>
                )
              ) : (
                <span className="chart-header-tip-empty">
                  {live && !hasAny
                    ? 'Waiting for volume bars…'
                    : 'No trade volume in window'}
                </span>
              )}
            </div>
          )}
        </div>
        {(!collapsed || lightbox) && (
          <div className="chart-volume-legend" aria-hidden>
            {mode === 'binance' ? (
              <>
                <span>
                  <i style={{ background: '#10b981' }} /> Buy
                </span>
                <span>
                  <i style={{ background: '#ef4444' }} /> Sell
                </span>
              </>
            ) : (
              <>
                <span>
                  <i style={{ background: '#10b981' }} /> Buy
                </span>
                <span>
                  <i style={{ background: '#ef4444' }} /> Sell
                </span>
                <span className="chart-volume-legend-note">Up ↑ / Down ↓</span>
              </>
            )}
          </div>
        )}
      </div>
      {(!collapsed || lightbox) && (
      <div
        className={`chart-wrap chart-wrap-volume${
          mode === 'outcomes' ? ' chart-wrap-volume-outcomes' : ''
        }${lightbox ? ' chart-wrap-lightbox' : ''}`}
        tabIndex={-1}
        onMouseDown={(e) => {
          // Keep click from focusing the SVG (browser black focus rect).
          e.preventDefault()
        }}
      >
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart
            data={visibleData}
            margin={CHART_MARGIN}
            syncId="market-price-charts"
            barCategoryGap="8%"
            barGap={2}
            onMouseMove={(state) => {
              if (!onHoverTimeChange) return
              const label = (state as { activeLabel?: string | number })?.activeLabel
              if (label == null) return
              const t = Number(label)
              if (Number.isFinite(t)) onHoverTimeChange(t)
            }}
            onMouseLeave={() => onHoverTimeChange?.(null)}
          >
            <CartesianGrid strokeDasharray="3 3" stroke="#e5e7eb" vertical={false} />
            <XAxis
              type="number"
              dataKey="t"
              domain={xDomain}
              ticks={timeTicks}
              tickFormatter={formatTimeTick}
              tick={{ fontSize: 10, fill: '#9ca3af' }}
              axisLine={false}
              tickLine={false}
              allowDataOverflow
            />
            <YAxis
              orientation="right"
              domain={yDomain}
              allowDataOverflow
              width={Y_AXIS_WIDTH}
              tick={{ fontSize: 10, fill: '#9ca3af' }}
              axisLine={false}
              tickLine={false}
              tickFormatter={(v) => formatVol(Math.abs(Number(v)), mode)}
            />
            <Tooltip content={() => null} cursor={{ fill: 'rgba(156,163,175,0.12)' }} />
            {mode === 'outcomes' && (
              <ReferenceLine y={0} stroke="#d1d5db" strokeWidth={1} />
            )}
            <Bar
              dataKey="range"
              isAnimationActive={false}
              barSize={barSize}
              maxBarSize={64}
              shape={(mode === 'binance' ? binanceShape : outcomesShape) as never}
              fill="#10b981"
              stroke="none"
            />
          </ComposedChart>
        </ResponsiveContainer>
      </div>
      )}
    </div>
    {!lightbox && enlarged
      ? createPortal(
          <div
            className="chart-lightbox-backdrop"
            role="presentation"
            onClick={() => setEnlarged(false)}
          >
            <div
              className="chart-lightbox"
              role="dialog"
              aria-modal="true"
              aria-label={`Large ${chartLabel}`}
              onClick={(ev) => ev.stopPropagation()}
            >
              <VolumeChart
                {...props}
                lightbox
                onLightboxClose={() => setEnlarged(false)}
              />
            </div>
          </div>,
          document.body,
        )
      : null}
    </>
  )
}
