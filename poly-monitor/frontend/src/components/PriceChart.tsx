import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { formatCents } from '../api'

export type BtcSeriesKey = 'twap' | 'chainlink' | 'binance'

export type BtcSeriesVisibility = Record<BtcSeriesKey, boolean>

export type TimeDomain = [number, number]

type Point = {
  t: number
  btc?: number | null
  twap?: number | null
  chainlink?: number | null
  up?: number | null
  down?: number | null
}

type Props = {
  data: Point[]
  priceToBeat?: number | null
  /** btc = Bitcoin prices; outcomes = Up/Down probabilities */
  mode?: 'btc' | 'outcomes'
  title?: string
  /** Shared numeric time domain (ms) so BTC and Up/Down charts align */
  xDomain: TimeDomain
  onXDomainChange: (next: TimeDomain) => void
  onXDomainReset?: () => void
  /** Full data time range — used for clamp / max zoom-out */
  xFullDomain: TimeDomain
  /** Default view (trailing window) — used for reset */
  xDefaultDomain: TimeDomain
  /** Which BTC series to draw (mode=btc) */
  seriesVisible?: BtcSeriesVisibility
  onSeriesVisibleChange?: (next: BtcSeriesVisibility) => void
  /** Shared hover timestamp (ms) — keeps BTC / Up-Down tooltips aligned */
  hoverTime?: number | null
  onHoverTimeChange?: (t: number | null) => void
}

const SERIES_META: {
  key: BtcSeriesKey
  dataKey: 'twap' | 'chainlink' | 'btc'
  label: string
  color: string
}[] = [
  { key: 'twap', dataKey: 'twap', label: 'Current Price', color: '#eab308' },
  { key: 'chainlink', dataKey: 'chainlink', label: 'Chainlink BTC', color: '#22c55e' },
  { key: 'binance', dataKey: 'btc', label: 'Binance BTC', color: '#2563eb' },
]

/** Keep plot areas aligned across BTC / Up-Down charts */
const Y_AXIS_WIDTH = 72
const CHART_MARGIN = { top: 10, right: 8, left: 4, bottom: 36 }
const TWAP_COLOR = '#eab308'
const FLOAT_TIP_ORANGE = TWAP_COLOR

type TargetLabelProps = {
  viewBox?: { x?: number; y?: number; width?: number }
  above: boolean | null
}

function TargetTagLabel({ viewBox, above }: TargetLabelProps) {
  if (!viewBox || viewBox.x == null || viewBox.y == null || viewBox.width == null) return null
  const w = above == null ? 62 : 78
  const h = 22
  const x = viewBox.x + viewBox.width - w + 2
  const y = viewBox.y - h / 2
  const arrows = above == null ? null : above ? '⇈' : '⇊'

  return (
    <g transform={`translate(${x}, ${y})`} className="chart-target-label">
      <rect width={w} height={h} rx={11} ry={11} fill="#4b5563" />
      <text x={12} y={15} fill="#ffffff" fontSize={11} fontWeight={650} fontFamily="inherit">
        Target
      </text>
      {arrows && (
        <text
          x={w - 14}
          y={15.5}
          fill="#ffffff"
          fontSize={12}
          fontWeight={700}
          textAnchor="middle"
          fontFamily="inherit"
        >
          {arrows}
        </text>
      )}
    </g>
  )
}

function formatTimeTick(ms: number): string {
  return new Date(ms).toLocaleTimeString(undefined, {
    hour: 'numeric',
    minute: '2-digit',
    second: '2-digit',
  })
}

/** Absolute clock-aligned ticks so labels scroll with the data (not fixed slots). */
function buildTimeTicks(domain: TimeDomain, targetCount = 5): number[] {
  const [x0, x1] = domain
  const span = Math.max(1, x1 - x0)
  const steps = [5_000, 10_000, 15_000, 30_000, 60_000, 120_000, 300_000]
  let step = steps[steps.length - 1]
  for (const candidate of steps) {
    if (span / candidate <= targetCount) {
      step = candidate
      break
    }
  }
  const first = Math.ceil(x0 / step) * step
  const ticks: number[] = []
  for (let t = first; t <= x1 + 1; t += step) {
    if (t >= x0 && t <= x1) ticks.push(t)
  }
  return ticks
}

type TipRow = { label: string; color: string; valueText: string; dataKey: string }

type HoverTip = {
  time: string
  rows: TipRow[]
}

function formatTipValue(mode: 'btc' | 'outcomes', raw: unknown): string {
  const num = raw == null || raw === '' ? null : Number(raw)
  if (num == null || !Number.isFinite(num)) return '—'
  if (mode === 'btc') {
    return `$${num.toLocaleString(undefined, {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })}`
  }
  return formatCents(num / 100)
}

function formatTipDateTime(ms: number): string {
  return new Date(ms).toLocaleString(undefined, {
    month: 'short',
    day: '2-digit',
    year: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    second: '2-digit',
  })
}

type ChartDatum = Point & { upPct?: number | null; downPct?: number | null }

function findNearestPoint(data: ChartDatum[], t: number): ChartDatum | null {
  if (!data.length) return null
  let best = data[0]
  let bestDist = Math.abs(data[0].t - t)
  for (let i = 1; i < data.length; i++) {
    const d = Math.abs(data[i].t - t)
    if (d < bestDist) {
      best = data[i]
      bestDist = d
    }
  }
  return best
}

function tipFromDataAtTime(
  mode: 'btc' | 'outcomes',
  data: ChartDatum[],
  t: number,
  seriesVisible: BtcSeriesVisibility,
): HoverTip | null {
  const point = findNearestPoint(data, t)
  if (!point) return null
  const rows: TipRow[] = []
  if (mode === 'btc') {
    for (const s of SERIES_META) {
      if (!seriesVisible[s.key]) continue
      const v = point[s.dataKey]
      if (v == null || !Number.isFinite(Number(v))) continue
      rows.push({
        label: s.label,
        color: s.color,
        dataKey: s.dataKey,
        valueText: formatTipValue('btc', v),
      })
    }
  } else {
    if (point.upPct != null && Number.isFinite(point.upPct)) {
      rows.push({
        label: 'Up',
        color: '#10b981',
        dataKey: 'upPct',
        valueText: formatTipValue('outcomes', point.upPct),
      })
    }
    if (point.downPct != null && Number.isFinite(point.downPct)) {
      rows.push({
        label: 'Down',
        color: '#ef4444',
        dataKey: 'downPct',
        valueText: formatTipValue('outcomes', point.downPct),
      })
    }
  }
  return {
    time: formatTipDateTime(point.t),
    rows,
  }
}

/** Fixed header tip (above plot) — prices + timestamp. */
function ChartHeaderTip({ tip }: { tip: HoverTip | null }) {
  if (!tip?.rows.length) {
    return <div className="chart-header-tip chart-header-tip-empty">Hover chart for values</div>
  }

  return (
    <div className="chart-header-tip">
      <div className="chart-header-tip-prices">
        {tip.rows.map((row) => (
          <div key={row.label} className="chart-header-tip-price" style={{ color: row.color }}>
            {row.valueText}
          </div>
        ))}
      </div>
      <div className="chart-float-tip-time">{tip.time}</div>
    </div>
  )
}

function HaloDot({
  cx,
  cy,
  fill,
}: {
  cx?: number
  cy?: number
  fill?: string
}) {
  if (cx == null || cy == null) return null
  const color = fill ?? FLOAT_TIP_ORANGE
  return (
    <circle cx={cx} cy={cy} r={3.5} fill={color} stroke="#fff" strokeWidth={1.5} />
  )
}

/** Vertical grey crosshair only (no horizontal hover line). */
function ChartCrosshair(props: {
  points?: { x: number; y: number }[]
  height?: number
  top?: number
}) {
  const { points, height, top } = props
  const x = points?.[0]?.x
  if (x == null || height == null || top == null) return null

  return (
    <g className="recharts-tooltip-cursor" pointerEvents="none">
      <line x1={x} y1={top} x2={x} y2={top + height} stroke="#d1d5db" strokeWidth={1} />
    </g>
  )
}

const DEFAULT_VISIBLE: BtcSeriesVisibility = {
  twap: true,
  chainlink: false,
  binance: false,
}

function clampDomain(domain: TimeDomain, full: TimeDomain): TimeDomain {
  const [f0, f1] = full
  const span = Math.max(1, f1 - f0)
  let [a, b] = domain
  if (b < a) [a, b] = [b, a]
  const width = Math.max(span * 0.02, b - a)
  let mid = (a + b) / 2
  if (mid - width / 2 < f0) mid = f0 + width / 2
  if (mid + width / 2 > f1) mid = f1 - width / 2
  return [mid - width / 2, mid + width / 2]
}

function domainEqual(a: TimeDomain, b: TimeDomain): boolean {
  return Math.abs(a[0] - b[0]) < 1 && Math.abs(a[1] - b[1]) < 1
}

export default function PriceChart({
  data,
  priceToBeat,
  mode = 'btc',
  title,
  xDomain,
  onXDomainChange,
  onXDomainReset,
  xFullDomain,
  xDefaultDomain,
  seriesVisible = DEFAULT_VISIBLE,
  onSeriesVisibleChange,
  hoverTime: hoverTimeProp,
  onHoverTimeChange,
}: Props) {
  const showBtc = mode === 'btc'
  const twapFillId = `twapAreaFill-${mode}`
  const wrapRef = useRef<HTMLDivElement | null>(null)
  const dragRef = useRef<{
    x: number
    y: number
    xDomain: TimeDomain
    yDomain: [number, number]
    zone: 'price' | 'time' | 'plot'
  } | null>(null)
  const [hoverZone, setHoverZone] = useState<'price' | 'time' | 'plot'>('plot')
  const [localHoverTime, setLocalHoverTime] = useState<number | null>(null)
  const hoverTime = onHoverTimeChange ? (hoverTimeProp ?? null) : localHoverTime
  const setHoverTime = onHoverTimeChange ?? setLocalHoverTime

  const chartData = useMemo(() => {
    const mapped = data.map((d) => ({
      ...d,
      // undefined (not null): Recharts treats null as 0 on Line charts
      upPct: d.up != null && Number.isFinite(d.up) ? d.up * 100 : undefined,
      downPct: d.down != null && Number.isFinite(d.down) ? d.down * 100 : undefined,
    }))
    if (mode !== 'outcomes') return mapped
    let i = 0
    while (
      i < mapped.length &&
      mapped[i].upPct == null &&
      mapped[i].downPct == null
    ) {
      i += 1
    }
    let j = mapped.length
    while (j > i && mapped[j - 1].upPct == null && mapped[j - 1].downPct == null) {
      j -= 1
    }
    return i || j < mapped.length ? mapped.slice(i, j) : mapped
  }, [data, mode])

  // When TWAP is frozen/missing (stalled RTDS), plot Binance so the chart isn't blank.
  const plotVisible = useMemo((): BtcSeriesVisibility => {
    if (!showBtc) return seriesVisible
    const twapVals = chartData
      .map((d) => d.twap)
      .filter((v): v is number => v != null && Number.isFinite(v))
    const twapSpan =
      twapVals.length >= 2 ? Math.max(...twapVals) - Math.min(...twapVals) : 0
    const twapDead = twapVals.length < 2 || twapSpan < 1
    if (!twapDead) return seriesVisible
    const hasBinance = chartData.some((d) => d.btc != null && Number.isFinite(Number(d.btc)))
    if (!hasBinance) return seriesVisible
    return { ...seriesVisible, twap: false, binance: true }
  }, [showBtc, seriesVisible, chartData])

  const hoverTip = useMemo(
    () =>
      hoverTime == null
        ? null
        : tipFromDataAtTime(mode, chartData, hoverTime, plotVisible),
    [
      hoverTime,
      mode,
      chartData,
      plotVisible.twap,
      plotVisible.chainlink,
      plotVisible.binance,
    ],
  )

  const onChartMouseMove = (state: {
    activeLabel?: string | number
    isTooltipActive?: boolean
  }) => {
    if (!state?.isTooltipActive || state.activeLabel == null) return
    const t = Number(state.activeLabel)
    if (!Number.isFinite(t)) return
    setHoverTime(t)
  }

  const clearHover = () => setHoverTime(null)

  const timeTicks = useMemo(() => buildTimeTicks(xDomain), [xDomain])

  const autoY = useMemo((): [number, number] => {
    if (!showBtc) return [0, 100]
    const values = chartData.flatMap((d) =>
      SERIES_META.filter((s) => plotVisible[s.key])
        .map((s) => d[s.dataKey])
        .filter((v): v is number => v != null && Number.isFinite(v)),
    )
    const lo = values.length ? Math.min(...values) : 0
    const hi = values.length ? Math.max(...values) : 1
    const pad = Math.max((hi - lo) * 0.15, 5)
    const y0 = priceToBeat != null ? Math.min(lo - pad, priceToBeat - pad * 0.35) : lo - pad
    const y1 = priceToBeat != null ? Math.max(hi + pad, priceToBeat + pad * 0.35) : hi + pad
    return [y0, y1]
  }, [
    chartData,
    priceToBeat,
    showBtc,
    plotVisible.twap,
    plotVisible.chainlink,
    plotVisible.binance,
  ])

  // null = follow autoY; set when user zooms/pans vertically
  const [yZoom, setYZoom] = useState<[number, number] | null>(null)
  const yDomain = yZoom ?? autoY

  const lastTwap =
    [...chartData].reverse().find((d) => d.twap != null)?.twap ??
    [...chartData].reverse().find((d) => d.btc != null)?.btc ??
    null
  const aboveTarget =
    lastTwap != null && priceToBeat != null ? lastTwap >= priceToBeat : null

  const xZoomed = !domainEqual(xDomain, xDefaultDomain)
  const yZoomed = yZoom != null
  const canReset = xZoomed || yZoomed

  const dataKey = `${data[0]?.t ?? 0}-${data[data.length - 1]?.t ?? 0}-${data.length}`
  useEffect(() => {
    setYZoom(null)
  }, [dataKey])

  const resetZoom = () => {
    if (onXDomainReset) onXDomainReset()
    else onXDomainChange(xDefaultDomain)
    setYZoom(null)
  }

  const toggle = (key: BtcSeriesKey) => {
    if (!onSeriesVisibleChange) return
    const next = { ...seriesVisible, [key]: !seriesVisible[key] }
    if (!next.twap && !next.chainlink && !next.binance) return
    onSeriesVisibleChange(next)
  }

  const hitZone = (clientX: number, clientY: number): 'price' | 'time' | 'plot' => {
    const el = wrapRef.current
    if (!el) return 'plot'
    const rect = el.getBoundingClientRect()
    const plotRight = rect.right - CHART_MARGIN.right - Y_AXIS_WIDTH
    const plotBottom = rect.bottom - CHART_MARGIN.bottom
    // Bottom-right corner is the reset control — treat as plot for cursor.
    if (clientX >= plotRight && clientY >= plotBottom) return 'plot'
    if (clientX >= plotRight) return 'price'
    if (clientY >= plotBottom) return 'time'
    return 'plot'
  }

  const onPointerDown = (ev: React.PointerEvent) => {
    if (ev.button !== 0) return
    const zone = hitZone(ev.clientX, ev.clientY)
    setHoverZone(zone)
    ;(ev.currentTarget as HTMLElement).setPointerCapture(ev.pointerId)
    dragRef.current = {
      x: ev.clientX,
      y: ev.clientY,
      xDomain: [...xDomain] as TimeDomain,
      yDomain: [...yDomain] as [number, number],
      zone,
    }
  }

  const onPointerMove = (ev: React.PointerEvent) => {
    const zone = hitZone(ev.clientX, ev.clientY)
    if (!dragRef.current) {
      setHoverZone((z) => (z === zone ? z : zone))
    }
    const drag = dragRef.current
    const el = wrapRef.current
    if (!drag || !el) return
    const rect = el.getBoundingClientRect()
    const plotW = Math.max(1, rect.width - CHART_MARGIN.left - CHART_MARGIN.right - Y_AXIS_WIDTH)
    const plotH = Math.max(1, rect.height - CHART_MARGIN.top - CHART_MARGIN.bottom)
    const dx = ev.clientX - drag.x
    const dy = ev.clientY - drag.y
    const dragZone = drag.zone
    const fullXSpan = Math.max(1, xFullDomain[1] - xFullDomain[0])
    const minXSpan = fullXSpan * 0.02
    const minYSpan = showBtc ? 1 : 0.5

    // X-axis strip: drag left/right to scale time (right = zoom in, left = zoom out).
    if (dragZone === 'time') {
      const [x0, x1] = drag.xDomain
      const xSpan = Math.max(1, x1 - x0)
      const factor = Math.exp((-dx / plotW) * 2.2)
      const nextSpan = Math.min(fullXSpan, Math.max(minXSpan, xSpan * factor))
      const mid = (x0 + x1) / 2
      onXDomainChange(
        clampDomain([mid - nextSpan / 2, mid + nextSpan / 2], xFullDomain),
      )
      return
    }

    // Y-axis strip: drag up/down to scale price (up = zoom in, down = zoom out).
    if (dragZone === 'price') {
      const [y0, y1] = drag.yDomain
      const ySpan = Math.max(minYSpan, y1 - y0)
      const factor = Math.exp((dy / plotH) * 2.2)
      const nextSpan = Math.max(minYSpan, ySpan * factor)
      const mid = (y0 + y1) / 2
      const next0 = mid - nextSpan / 2
      const next1 = mid + nextSpan / 2
      if (showBtc) {
        setYZoom([next0, next1])
      } else {
        setYZoom([Math.max(0, next0), Math.min(100, next1)])
      }
      return
    }

    // Plot: drag to pan.
    if (dragZone === 'plot') {
      const [x0, x1] = drag.xDomain
      const xSpan = x1 - x0
      const xShift = (-dx / plotW) * xSpan
      onXDomainChange(clampDomain([x0 + xShift, x1 + xShift], xFullDomain))

      const [y0, y1] = drag.yDomain
      const ySpan = y1 - y0
      const yShift = (dy / plotH) * ySpan
      if (showBtc) {
        setYZoom([y0 + yShift, y1 + yShift])
      } else {
        setYZoom([Math.max(0, y0 + yShift), Math.min(100, y1 + yShift)])
      }
    }
  }

  const onPointerUp = (ev: React.PointerEvent) => {
    dragRef.current = null
    try {
      ;(ev.currentTarget as HTMLElement).releasePointerCapture(ev.pointerId)
    } catch {
      /* ignore */
    }
  }

  return (
    <div className="chart-block">
      <div className="chart-header">
        <div className="chart-header-left">
          {title && <div className="chart-title">{title}</div>}
          <ChartHeaderTip tip={hoverTip} />
        </div>
        <div className="chart-header-right">
          {showBtc && onSeriesVisibleChange ? (
            <div className="chart-series-toggles" role="group" aria-label="BTC series visibility">
              {SERIES_META.map((s) => (
                <label
                  key={s.key}
                  className={`chart-series-toggle ${plotVisible[s.key] ? 'on' : ''}`}
                >
                  <input
                    type="checkbox"
                    checked={plotVisible[s.key]}
                    onChange={() => toggle(s.key)}
                  />
                  <span className="chart-series-swatch" style={{ background: s.color }} />
                  {s.label}
                </label>
              ))}
            </div>
          ) : null}
        </div>
      </div>
      <div
        className={`chart-wrap chart-wrap-zoom chart-cursor-${hoverZone}`}
        ref={wrapRef}
        tabIndex={-1}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
        onPointerLeave={() => {
          if (!dragRef.current) setHoverZone('plot')
          clearHover()
        }}
        onDoubleClick={resetZoom}
        title="Drag left/right on time axis to zoom time · drag up/down on price axis to zoom price · drag plot to pan · double-click / Reset to restore"
      >
        <div className="chart-zoom-zone chart-zoom-zone-time" aria-hidden />
        <div className="chart-zoom-zone chart-zoom-zone-price" aria-hidden />
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart
            data={chartData}
            margin={CHART_MARGIN}
            syncId="market-price-charts"
            syncMethod="value"
            onMouseMove={onChartMouseMove}
            onMouseLeave={clearHover}
          >
            <CartesianGrid stroke="#eef0f4" strokeDasharray="0" horizontal vertical />
            <XAxis
              type="number"
              dataKey="t"
              domain={[xDomain[0], xDomain[1]]}
              ticks={timeTicks}
              interval={0}
              allowDataOverflow
              stroke="#9ca3af"
              tick={{ fontSize: 11, fill: '#9ca3af' }}
              axisLine={false}
              tickLine={false}
              tickFormatter={(v) => formatTimeTick(Number(v))}
            />
            {showBtc ? (
              <>
                <defs>
                  <linearGradient id={twapFillId} x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor={TWAP_COLOR} stopOpacity={0.1} />
                    <stop offset="70%" stopColor={TWAP_COLOR} stopOpacity={0.03} />
                    <stop offset="100%" stopColor={TWAP_COLOR} stopOpacity={0} />
                  </linearGradient>
                </defs>
                <YAxis
                  orientation="right"
                  domain={yDomain}
                  allowDataOverflow
                  stroke="#9ca3af"
                  tick={{ fontSize: 11, fill: '#9ca3af' }}
                  width={Y_AXIS_WIDTH}
                  axisLine={false}
                  tickLine={false}
                  tickFormatter={(v) =>
                    `$${Number(v).toLocaleString(undefined, { maximumFractionDigits: 0 })}`
                  }
                />
                <Tooltip
                  content={() => null}
                  cursor={<ChartCrosshair />}
                  isAnimationActive={false}
                />
                {priceToBeat != null && (
                  <ReferenceLine
                    y={priceToBeat}
                    stroke="#9ca3af"
                    strokeDasharray="5 5"
                    strokeWidth={1.5}
                    ifOverflow="extendDomain"
                    label={<TargetTagLabel above={aboveTarget} />}
                  />
                )}
                {plotVisible.twap && (
                  <>
                    <Area
                      type="monotone"
                      dataKey="twap"
                      name="Current Price"
                      stroke="none"
                      fill={`url(#${twapFillId})`}
                      fillOpacity={1}
                      dot={false}
                      activeDot={false}
                      isAnimationActive={false}
                      connectNulls
                      baseValue={yDomain[0]}
                      legendType="none"
                      tooltipType="none"
                    />
                    <Line
                      type="monotone"
                      dataKey="twap"
                      name="Current Price"
                      stroke={TWAP_COLOR}
                      strokeWidth={2.35}
                      dot={false}
                      activeDot={(dotProps) => (
                        <HaloDot cx={dotProps.cx} cy={dotProps.cy} fill={TWAP_COLOR} />
                      )}
                      isAnimationActive={false}
                      connectNulls
                    />
                  </>
                )}
                {SERIES_META.filter((s) => s.key !== 'twap').map((s) =>
                  plotVisible[s.key] ? (
                    <Line
                      key={s.key}
                      type="monotone"
                      dataKey={s.dataKey}
                      name={s.label}
                      stroke={s.color}
                      dot={false}
                      activeDot={(dotProps) => (
                        <HaloDot cx={dotProps.cx} cy={dotProps.cy} fill={s.color} />
                      )}
                      strokeWidth={1.85}
                      isAnimationActive={false}
                      connectNulls
                    />
                  ) : null,
                )}
              </>
            ) : (
              <>
                <YAxis
                  orientation="right"
                  domain={yDomain}
                  allowDataOverflow
                  stroke="#9ca3af"
                  tick={{ fontSize: 11, fill: '#9ca3af' }}
                  width={Y_AXIS_WIDTH}
                  axisLine={false}
                  tickLine={false}
                  tickFormatter={(v) => `${Number(v).toFixed(2)}¢`}
                />
                <Tooltip
                  content={() => null}
                  cursor={<ChartCrosshair />}
                  isAnimationActive={false}
                />
                <Line
                  type="monotone"
                  dataKey="upPct"
                  name="Up"
                  stroke="#10b981"
                  dot={false}
                  activeDot={(dotProps) => (
                    <HaloDot cx={dotProps.cx} cy={dotProps.cy} fill="#10b981" />
                  )}
                  strokeWidth={2}
                  isAnimationActive={false}
                  connectNulls={false}
                />
                <Line
                  type="monotone"
                  dataKey="downPct"
                  name="Down"
                  stroke="#ef4444"
                  dot={false}
                  activeDot={(dotProps) => (
                    <HaloDot cx={dotProps.cx} cy={dotProps.cy} fill="#ef4444" />
                  )}
                  strokeWidth={2}
                  isAnimationActive={false}
                  connectNulls={false}
                />
              </>
            )}
          </ComposedChart>
        </ResponsiveContainer>
        {canReset && (
          <button
            type="button"
            className="chart-reset-corner"
            onClick={(e) => {
              e.stopPropagation()
              resetZoom()
            }}
            onPointerDown={(e) => e.stopPropagation()}
            aria-label="Reset zoom"
            title="Reset zoom"
          >
            <svg
              className="chart-reset-icon"
              viewBox="0 0 24 24"
              width="15"
              height="15"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden
            >
              <path d="M8 3H5a2 2 0 0 0-2 2v3" />
              <path d="M16 3h3a2 2 0 0 1 2 2v3" />
              <path d="M8 21H5a2 2 0 0 1-2-2v-3" />
              <path d="M16 21h3a2 2 0 0 0 2-2v-3" />
            </svg>
          </button>
        )}
      </div>
    </div>
  )
}
