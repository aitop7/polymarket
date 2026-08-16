import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Scatter,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { formatCents } from '../api'
import ChartCollapseButton from './ChartCollapseButton'

export type BtcSeriesKey = 'twap' | 'chainlink' | 'binance'

export type BtcSeriesVisibility = Record<BtcSeriesKey, boolean>

export type TimeDomain = [number, number]

/** Selected trader fills plotted on the outcomes chart. */
export type TraderMark = {
  t: number
  /** Price in cents (0–100), same scale as upPct/downPct */
  pricePct: number
  side: 'BUY' | 'SELL'
  outcome: 'Up' | 'Down'
}

type Point = {
  t: number
  btc?: number | null
  twap?: number | null
  chainlink?: number | null
  up?: number | null
  down?: number | null
}

type ChartPoint = Point & {
  upPct?: number
  downPct?: number
  upEma?: number
  downEma?: number
  upSavgol?: number
  downSavgol?: number
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
  /** History: selected trader buy/sell markers (outcomes mode) */
  traderMarks?: TraderMark[]
  /** External highlight (e.g. activity-row hover): vertical cursor + emphasized mark */
  highlightTime?: number | null
  /** Outcomes mode: show EMA overlays on Up/Down */
  showEma?: boolean
  onShowEmaChange?: (next: boolean) => void
  /** Outcomes mode: show Savitzky–Golay overlays on Up/Down */
  showSavgol?: boolean
  onShowSavgolChange?: (next: boolean) => void
  /** EMA period in samples (default 20) */
  emaPeriod?: number
  /** Savitzky–Golay window length (odd, default 11) */
  savgolWindow?: number
  /** Savitzky–Golay polynomial order (default 2) */
  savgolPoly?: number
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

const ET_TZ = 'America/New_York'

function formatTimeTick(ms: number): string {
  try {
    return new Intl.DateTimeFormat('en-US', {
      timeZone: ET_TZ,
      hour: 'numeric',
      minute: '2-digit',
      second: '2-digit',
      hour12: true,
    }).format(new Date(ms))
  } catch {
    return ''
  }
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

function formatTipDeltaUsd(delta: number): string {
  const abs = Math.abs(delta).toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })
  if (delta > 0) return `+${abs}`
  if (delta < 0) return `−${abs}`
  return abs
}

/** First finite value for a BTC series at/after the visible domain open. */
function openPriceForSeries(
  data: ChartDatum[],
  dataKey: 'twap' | 'chainlink' | 'btc',
  domain: TimeDomain | null | undefined,
): number | null {
  const t0 = domain?.[0]
  for (const d of data) {
    if (t0 != null && Number.isFinite(t0) && d.t < t0) continue
    const v = d[dataKey]
    if (v != null && Number.isFinite(Number(v))) return Number(v)
  }
  // Domain may start before the series exists — fall back to first sample anywhere.
  for (const d of data) {
    const v = d[dataKey]
    if (v != null && Number.isFinite(Number(v))) return Number(v)
  }
  return null
}

function formatTipValueWithDelta(
  mode: 'btc' | 'outcomes',
  raw: unknown,
  open: number | null,
): string {
  const price = formatTipValue(mode, raw)
  if (mode !== 'btc' || open == null) return price
  const num = raw == null || raw === '' ? null : Number(raw)
  if (num == null || !Number.isFinite(num)) return price
  return `${price} (${formatTipDeltaUsd(num - open)})`
}

function formatTipDateTime(ms: number): string {
  try {
    return `${new Intl.DateTimeFormat('en-US', {
      timeZone: ET_TZ,
      month: 'short',
      day: '2-digit',
      year: 'numeric',
      hour: 'numeric',
      minute: '2-digit',
      second: '2-digit',
      hour12: true,
    }).format(new Date(ms))} ET`
  } catch {
    return ''
  }
}

type ChartDatum = Point & {
  upPct?: number | null
  downPct?: number | null
  upEma?: number | null
  downEma?: number | null
  upSavgol?: number | null
  downSavgol?: number | null
}

const DEFAULT_EMA_PERIOD = 20
/** Odd window length for Savitzky–Golay (samples). */
const DEFAULT_SAVGOL_WINDOW = 11
const DEFAULT_SAVGOL_POLY = 2

/** Mid-range outcome (probability). Stubs at ≤2¢ / ≥98¢ are open placeholders. */
function isMidOutcome(v: number): boolean {
  return Number.isFinite(v) && v > 0.02 && v < 0.98
}

function isStubOutcome(v: number): boolean {
  return Number.isFinite(v) && (v <= 0.02 || v >= 0.98)
}

/**
 * Map Up/Down for Recharts without open 1¢/99¢ combs.
 * - Drop leading stubs until a mid-range quote exists
 * - Drop stub / huge jumps (leave a gap; do not forward-fill across them)
 * - Forward-fill only when this tick has no Up/Down quote (BTC-only rows)
 */
function mapOutcomesChartData(
  data: Point[],
  emaPeriod: number,
  savgolWindow = DEFAULT_SAVGOL_WINDOW,
  savgolPoly = DEFAULT_SAVGOL_POLY,
): ChartPoint[] {
  const MAX_JUMP_PCT = 45
  let seenMid = false
  let lastUpPct: number | undefined
  let lastDownPct: number | undefined
  const mapped: ChartPoint[] = []

  for (const d of data) {
    const rawUp = d.up != null && Number.isFinite(d.up) ? Number(d.up) : null
    const rawDown = d.down != null && Number.isFinite(d.down) ? Number(d.down) : null

    if (!seenMid) {
      if (
        (rawUp != null && isMidOutcome(rawUp)) ||
        (rawDown != null && isMidOutcome(rawDown))
      ) {
        seenMid = true
      }
    }

    const acceptSide = (
      raw: number | null,
      lastPct: number | undefined,
    ): number | undefined => {
      if (raw == null) return undefined
      if (!seenMid) return undefined
      if (isStubOutcome(raw)) {
        // Settling extremes only if we approached them gradually.
        if (lastPct == null || Math.abs(raw * 100 - lastPct) > MAX_JUMP_PCT) {
          return undefined
        }
      }
      const pct = raw * 100
      if (lastPct != null && Math.abs(pct - lastPct) > MAX_JUMP_PCT) {
        return undefined
      }
      return pct
    }

    let upPct: number | undefined
    let downPct: number | undefined

    if (rawUp == null && rawDown == null) {
      // Hollow / BTC-only timestamp — carry last good odds.
      upPct = lastUpPct
      downPct = lastDownPct
    } else {
      // Missing one side on this tick → carry; rejected stub/jump → leave gap.
      upPct = rawUp == null ? lastUpPct : acceptSide(rawUp, lastUpPct)
      downPct = rawDown == null ? lastDownPct : acceptSide(rawDown, lastDownPct)
      if (rawUp != null && upPct != null) lastUpPct = upPct
      if (rawDown != null && downPct != null) lastDownPct = downPct
    }

    mapped.push({ ...d, upPct, downPct })
  }

  let i = 0
  while (i < mapped.length && mapped[i].upPct == null && mapped[i].downPct == null) {
    i += 1
  }
  let j = mapped.length
  while (j > i && mapped[j - 1].upPct == null && mapped[j - 1].downPct == null) {
    j -= 1
  }
  const sliced = i || j < mapped.length ? mapped.slice(i, j) : mapped
  const upVals = sliced.map((d) => d.upPct)
  const downVals = sliced.map((d) => d.downPct)
  const upEma = emaSeries(upVals, emaPeriod)
  const downEma = emaSeries(downVals, emaPeriod)
  const upSavgol = savgolSeries(upVals, savgolWindow, savgolPoly)
  const downSavgol = savgolSeries(downVals, savgolWindow, savgolPoly)
  return sliced.map((d, idx) => ({
    ...d,
    upEma: upEma[idx],
    downEma: downEma[idx],
    upSavgol: upSavgol[idx],
    downSavgol: downSavgol[idx],
  }))
}

/** Point EMA over finite samples only; undefined until the first finite value. */
function emaSeries(values: Array<number | null | undefined>, period: number): Array<number | undefined> {
  const n = Math.max(1, Math.floor(period))
  const alpha = 2 / (n + 1)
  const out: Array<number | undefined> = new Array(values.length)
  let ema: number | null = null
  for (let i = 0; i < values.length; i++) {
    const v = values[i]
    if (v == null || !Number.isFinite(v)) {
      out[i] = ema ?? undefined
      continue
    }
    ema = ema == null ? v : alpha * v + (1 - alpha) * ema
    out[i] = ema
  }
  return out
}

/** Solve dense square system Ax=b via Gauss–Jordan (small polyorder only). */
function solveLinearSystem(aIn: number[][], bIn: number[]): number[] | null {
  const n = bIn.length
  const m = aIn.map((row, i) => [...row, bIn[i]])
  for (let col = 0; col < n; col++) {
    let pivot = col
    for (let r = col + 1; r < n; r++) {
      if (Math.abs(m[r][col]) > Math.abs(m[pivot][col])) pivot = r
    }
    if (Math.abs(m[pivot][col]) < 1e-12) return null
    if (pivot !== col) {
      const tmp = m[col]
      m[col] = m[pivot]
      m[pivot] = tmp
    }
    const div = m[col][col]
    for (let c = col; c <= n; c++) m[col][c] /= div
    for (let r = 0; r < n; r++) {
      if (r === col) continue
      const f = m[r][col]
      for (let c = col; c <= n; c++) m[r][c] -= f * m[col][c]
    }
  }
  return m.map((row) => row[n])
}

/** Convolution coeffs for Savitzky–Golay smoothing (0th derivative). */
function savgolCoeffs(window: number, polyorder: number): number[] | null {
  let w = Math.max(3, Math.floor(window))
  if (w % 2 === 0) w += 1
  let p = Math.max(0, Math.min(Math.floor(polyorder), w - 1))
  const half = (w - 1) / 2
  // A[i][j] = t^j, t = -half..half
  const ata: number[][] = Array.from({ length: p + 1 }, () => Array(p + 1).fill(0))
  const at: number[][] = Array.from({ length: p + 1 }, () => Array(w).fill(0))
  for (let i = 0; i < w; i++) {
    const t = i - half
    let pow = 1
    for (let j = 0; j <= p; j++) {
      at[j][i] = pow
      pow *= t
    }
  }
  for (let r = 0; r <= p; r++) {
    for (let c = 0; c <= p; c++) {
      let s = 0
      for (let i = 0; i < w; i++) s += at[r][i] * at[c][i]
      ata[r][c] = s
    }
  }
  // Want e0^T (AᵀA)⁻¹ Aᵀ → solve (AᵀA) x = e0, then coeffs = A x? 
  // Actually c = A (AᵀA)⁻¹ e0 where e0 = [1,0,0,...]
  const e0 = Array(p + 1).fill(0)
  e0[0] = 1
  const x = solveLinearSystem(ata, e0)
  if (!x) return null
  const coeffs = Array(w).fill(0)
  for (let i = 0; i < w; i++) {
    let s = 0
    for (let j = 0; j <= p; j++) s += at[j][i] * x[j]
    coeffs[i] = s
  }
  return coeffs
}

/**
 * Savitzky–Golay smooth over finite samples.
 * Null gaps are left as gaps (no bleed across jumps); edges use a shrinking window.
 */
function savgolSeries(
  values: Array<number | null | undefined>,
  window = DEFAULT_SAVGOL_WINDOW,
  polyorder = DEFAULT_SAVGOL_POLY,
): Array<number | undefined> {
  const out: Array<number | undefined> = new Array(values.length)
  const coeffs = savgolCoeffs(window, polyorder)
  if (!coeffs || values.length === 0) {
    return out.fill(undefined)
  }
  const w = coeffs.length
  const half = (w - 1) / 2
  for (let i = 0; i < values.length; i++) {
    if (values[i] == null || !Number.isFinite(Number(values[i]))) {
      out[i] = undefined
      continue
    }
    let sum = 0
    let weight = 0
    let ok = true
    for (let k = -half; k <= half; k++) {
      const j = i + k
      if (j < 0 || j >= values.length) {
        ok = false
        break
      }
      const v = values[j]
      if (v == null || !Number.isFinite(v)) {
        ok = false
        break
      }
      const c = coeffs[k + half]
      sum += c * v
      weight += c
    }
    if (!ok) {
      // Edge / gap: fall back to a smaller odd window centered on i when possible.
      const maxHalf = Math.min(i, values.length - 1 - i, half)
      if (maxHalf < 1) {
        out[i] = Number(values[i])
        continue
      }
      const w2 = maxHalf * 2 + 1
      const c2 = savgolCoeffs(w2, Math.min(polyorder, w2 - 1))
      if (!c2) {
        out[i] = Number(values[i])
        continue
      }
      let s2 = 0
      let good = true
      for (let k = -maxHalf; k <= maxHalf; k++) {
        const v = values[i + k]
        if (v == null || !Number.isFinite(v)) {
          good = false
          break
        }
        s2 += c2[k + maxHalf] * v
      }
      out[i] = good ? s2 : Number(values[i])
      continue
    }
    out[i] = weight !== 0 ? sum : Number(values[i])
  }
  return out
}

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
  showEma = false,
  xDomain?: TimeDomain | null,
  showSavgol = false,
): HoverTip | null {
  const point = findNearestPoint(data, t)
  if (!point) return null
  const rows: TipRow[] = []
  if (mode === 'btc') {
    for (const s of SERIES_META) {
      if (!seriesVisible[s.key]) continue
      const v = point[s.dataKey]
      if (v == null || !Number.isFinite(Number(v))) continue
      const open = openPriceForSeries(data, s.dataKey, xDomain)
      rows.push({
        label: s.label,
        color: s.color,
        dataKey: s.dataKey,
        valueText: formatTipValueWithDelta('btc', v, open),
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
    if (showEma && point.upEma != null && Number.isFinite(point.upEma)) {
      rows.push({
        label: 'Up EMA',
        color: '#059669',
        dataKey: 'upEma',
        valueText: formatTipValue('outcomes', point.upEma),
      })
    }
    if (showSavgol && point.upSavgol != null && Number.isFinite(point.upSavgol)) {
      rows.push({
        label: 'Up SG',
        color: '#0f766e',
        dataKey: 'upSavgol',
        valueText: formatTipValue('outcomes', point.upSavgol),
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
    if (showEma && point.downEma != null && Number.isFinite(point.downEma)) {
      rows.push({
        label: 'Down EMA',
        color: '#dc2626',
        dataKey: 'downEma',
        valueText: formatTipValue('outcomes', point.downEma),
      })
    }
    if (showSavgol && point.downSavgol != null && Number.isFinite(point.downSavgol)) {
      rows.push({
        label: 'Down SG',
        color: '#be123c',
        dataKey: 'downSavgol',
        valueText: formatTipValue('outcomes', point.downSavgol),
      })
    }
  }
  return {
    time: formatTipDateTime(point.t),
    rows,
  }
}

/** Last sample in (or at) the visible domain — live "now" / history final price. */
function defaultTipTime(
  data: ChartDatum[],
  domain: TimeDomain | null | undefined,
  mode: 'btc' | 'outcomes',
  seriesVisible: BtcSeriesVisibility,
  showEma = false,
  showSavgol = false,
): number | null {
  if (!data.length) return null
  const t1 = domain?.[1]

  const hasValues = (p: ChartDatum): boolean => {
    if (mode === 'btc') {
      for (const s of SERIES_META) {
        if (!seriesVisible[s.key]) continue
        const v = p[s.dataKey]
        if (v != null && Number.isFinite(Number(v))) return true
      }
      return false
    }
    if (p.upPct != null && Number.isFinite(p.upPct)) return true
    if (p.downPct != null && Number.isFinite(p.downPct)) return true
    if (showEma && p.upEma != null && Number.isFinite(p.upEma)) return true
    if (showEma && p.downEma != null && Number.isFinite(p.downEma)) return true
    if (showSavgol && p.upSavgol != null && Number.isFinite(p.upSavgol)) return true
    if (showSavgol && p.downSavgol != null && Number.isFinite(p.downSavgol)) return true
    return false
  }

  for (let i = data.length - 1; i >= 0; i--) {
    const t = Number(data[i].t)
    if (!Number.isFinite(t)) continue
    if (t1 != null && Number.isFinite(t1) && t > t1) continue
    if (!hasValues(data[i])) continue
    return t
  }
  const last = Number(data[data.length - 1].t)
  return Number.isFinite(last) ? last : null
}

/** Fixed header tip (above plot) — prices + timestamp. */
function ChartHeaderTip({ tip }: { tip: HoverTip | null }) {
  if (!tip?.rows.length) {
    return <div className="chart-header-tip chart-header-tip-empty">No price samples yet</div>
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

/** Buy = upward triangle; Sell = downward triangle. Color by outcome. */
function TraderMarkShape(props: {
  cx?: number
  cy?: number
  payload?: TraderMark & { active?: boolean }
}) {
  const { cx, cy, payload } = props
  if (cx == null || cy == null || !payload) return null
  const fill = payload.outcome === 'Up' ? '#10b981' : '#ef4444'
  const buy = payload.side === 'BUY'
  const active = Boolean(payload.active)
  const s = active ? 9 : 5.5
  const stroke = active ? '#0f1117' : '#fff'
  const strokeWidth = active ? 2 : 1
  const ring = active ? (
    <circle
      cx={cx}
      cy={cy}
      r={s + 5}
      fill="none"
      stroke={fill}
      strokeWidth={2}
      strokeOpacity={0.45}
    />
  ) : null
  if (buy) {
    return (
      <g>
        {ring}
        <polygon
          points={`${cx},${cy - s} ${cx - s},${cy + s * 0.7} ${cx + s},${cy + s * 0.7}`}
          fill={fill}
          stroke={stroke}
          strokeWidth={strokeWidth}
        />
      </g>
    )
  }
  return (
    <g>
      {ring}
      <polygon
        points={`${cx},${cy + s} ${cx - s},${cy - s * 0.7} ${cx + s},${cy - s * 0.7}`}
        fill={fill}
        stroke={stroke}
        strokeWidth={strokeWidth}
      />
    </g>
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
  traderMarks = [],
  highlightTime = null,
  showEma = false,
  onShowEmaChange,
  showSavgol = false,
  onShowSavgolChange,
  emaPeriod = DEFAULT_EMA_PERIOD,
  savgolWindow = DEFAULT_SAVGOL_WINDOW,
  savgolPoly = DEFAULT_SAVGOL_POLY,
}: Props) {
  const showBtc = mode === 'btc'
  const showSmooth = showEma || showSavgol
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
  const [collapsed, setCollapsed] = useState(false)
  const hoverTime = onHoverTimeChange
    ? (highlightTime ?? hoverTimeProp ?? null)
    : (highlightTime ?? localHoverTime)
  const setHoverTime = onHoverTimeChange ?? setLocalHoverTime

  const chartData = useMemo((): ChartPoint[] => {
    if (mode !== 'outcomes') {
      return data.map((d) => ({
        ...d,
        upPct: undefined,
        downPct: undefined,
      }))
    }
    return mapOutcomesChartData(data, emaPeriod, savgolWindow, savgolPoly)
  }, [data, mode, emaPeriod, savgolWindow, savgolPoly])

  // Prefer TWAP; then Chainlink; only fall back to Binance when neither exists.
  // Apply once into seriesVisible so toggles stay clickable (don't fight a derived plotVisible).
  const autoFallbackDone = useRef(false)
  const chartIdentity = `${chartData[0]?.t ?? 0}-${chartData.length}-${chartData[chartData.length - 1]?.t ?? 0}`
  useEffect(() => {
    autoFallbackDone.current = false
  }, [chartIdentity])

  useEffect(() => {
    if (!showBtc || !onSeriesVisibleChange || autoFallbackDone.current) return
    if (chartData.length === 0) return
    const hasTwap = chartData.some((d) => d.twap != null && Number.isFinite(Number(d.twap)))
    if (hasTwap) {
      autoFallbackDone.current = true
      return
    }
    // Only auto-switch from the default "TWAP only" preference.
    if (!seriesVisible.twap || seriesVisible.chainlink || seriesVisible.binance) {
      autoFallbackDone.current = true
      return
    }
    const hasChainlink = chartData.some(
      (d) => d.chainlink != null && Number.isFinite(Number(d.chainlink)),
    )
    if (hasChainlink) {
      autoFallbackDone.current = true
      onSeriesVisibleChange({ twap: false, chainlink: true, binance: false })
      return
    }
    const hasBinance = chartData.some((d) => d.btc != null && Number.isFinite(Number(d.btc)))
    if (hasBinance) {
      autoFallbackDone.current = true
      onSeriesVisibleChange({ twap: false, chainlink: false, binance: true })
    }
  }, [showBtc, chartData, seriesVisible, onSeriesVisibleChange])

  const plotVisible = seriesVisible

  const hoverTip = useMemo(() => {
    // Default: live current / history last price. Hover overrides without
    // sticking a crosshair when the pointer leaves.
    const tipTime =
      hoverTime ?? defaultTipTime(chartData, xDomain, mode, plotVisible, showEma, showSavgol)
    if (tipTime == null) return null
    return tipFromDataAtTime(
      mode,
      chartData,
      tipTime,
      plotVisible,
      showEma,
      xDomain,
      showSavgol,
    )
  }, [
    hoverTime,
    mode,
    chartData,
    plotVisible.twap,
    plotVisible.chainlink,
    plotVisible.binance,
    showEma,
    showSavgol,
    xDomain,
  ])

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
    const span = Math.max(0, hi - lo)
    // Quiet tapes move by cents; a hard $5 pad made Binance look frozen.
    const rel = Math.max(Math.abs(lo), Math.abs(hi), 1) * 0.00002
    const pad = Math.max(span * 0.15, Math.min(5, Math.max(0.25, rel)))
    let y0 = lo - pad
    let y1 = hi + pad
    // Keep Price-to-Beat in view only when it's near the series; a distant
    // strike (Binance vs Chainlink divergence) used to flatten the line.
    if (priceToBeat != null && Number.isFinite(priceToBeat) && values.length) {
      const mid = (lo + hi) / 2
      const near = Math.abs(priceToBeat - mid) <= Math.max(40, Math.abs(mid) * 0.0015)
      if (near) {
        y0 = Math.min(y0, priceToBeat - pad * 0.35)
        y1 = Math.max(y1, priceToBeat + pad * 0.35)
      }
    }
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
    // Prevent browser focus ring on the chart SVG / wrapper.
    ev.preventDefault()
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
    <div className={`chart-block${collapsed ? ' chart-block-collapsed' : ''}`}>
      <div className="chart-header">
        <div className="chart-header-left">
          <div className="chart-title-row">
            <ChartCollapseButton
              collapsed={collapsed}
              onToggle={() => setCollapsed((v) => !v)}
              label={title || (showBtc ? 'BTC price' : 'Up / Down price')}
            />
            {title && <div className="chart-title">{title}</div>}
          </div>
          {!collapsed && <ChartHeaderTip tip={hoverTip} />}
        </div>
        <div className="chart-header-right">
          {!collapsed && showBtc && onSeriesVisibleChange ? (
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
          {!collapsed && !showBtc && (onShowEmaChange || onShowSavgolChange) ? (
            <div className="chart-series-toggles" role="group" aria-label="Outcomes series visibility">
              {onShowEmaChange ? (
                <label className={`chart-series-toggle ${showEma ? 'on' : ''}`}>
                  <input
                    type="checkbox"
                    checked={showEma}
                    onChange={() => onShowEmaChange(!showEma)}
                  />
                  <span
                    className="chart-series-swatch"
                    style={{ background: '#059669' }}
                  />
                  EMA
                </label>
              ) : null}
              {onShowSavgolChange ? (
                <label className={`chart-series-toggle ${showSavgol ? 'on' : ''}`}>
                  <input
                    type="checkbox"
                    checked={showSavgol}
                    onChange={() => onShowSavgolChange(!showSavgol)}
                  />
                  <span
                    className="chart-series-swatch"
                    style={{ background: '#0f766e' }}
                  />
                  SG
                </label>
              ) : null}
            </div>
          ) : null}
        </div>
      </div>
      {!collapsed && (
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
                {highlightTime != null && (
                  <ReferenceLine
                    x={highlightTime}
                    stroke="#3b82f6"
                    strokeWidth={1.75}
                    strokeDasharray="4 3"
                    ifOverflow="hidden"
                  />
                )}
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
                {highlightTime != null && (
                  <ReferenceLine
                    x={highlightTime}
                    stroke="#3b82f6"
                    strokeWidth={1.75}
                    strokeDasharray="4 3"
                    ifOverflow="hidden"
                  />
                )}
                <Line
                  type="monotone"
                  dataKey="upPct"
                  name="Up"
                  stroke="#10b981"
                  strokeOpacity={showSmooth ? 0.45 : 1}
                  strokeDasharray={showSmooth ? '4 3' : undefined}
                  dot={false}
                  activeDot={(dotProps) => (
                    <HaloDot cx={dotProps.cx} cy={dotProps.cy} fill="#10b981" />
                  )}
                  strokeWidth={showSmooth ? 1.5 : 2}
                  isAnimationActive={false}
                  connectNulls={false}
                />
                <Line
                  type="monotone"
                  dataKey="downPct"
                  name="Down"
                  stroke="#ef4444"
                  strokeOpacity={showSmooth ? 0.45 : 1}
                  strokeDasharray={showSmooth ? '4 3' : undefined}
                  dot={false}
                  activeDot={(dotProps) => (
                    <HaloDot cx={dotProps.cx} cy={dotProps.cy} fill="#ef4444" />
                  )}
                  strokeWidth={showSmooth ? 1.5 : 2}
                  isAnimationActive={false}
                  connectNulls={false}
                />
                {showEma ? (
                  <>
                    <Line
                      type="monotone"
                      dataKey="upEma"
                      name="Up EMA"
                      stroke="#059669"
                      strokeOpacity={1}
                      dot={false}
                      activeDot={false}
                      strokeWidth={2.35}
                      isAnimationActive={false}
                      connectNulls
                      legendType="none"
                    />
                    <Line
                      type="monotone"
                      dataKey="downEma"
                      name="Down EMA"
                      stroke="#dc2626"
                      strokeOpacity={1}
                      dot={false}
                      activeDot={false}
                      strokeWidth={2.35}
                      isAnimationActive={false}
                      connectNulls
                      legendType="none"
                    />
                  </>
                ) : null}
                {showSavgol ? (
                  <>
                    <Line
                      type="monotone"
                      dataKey="upSavgol"
                      name="Up SG"
                      stroke="#0f766e"
                      strokeOpacity={1}
                      strokeDasharray="6 3"
                      dot={false}
                      activeDot={false}
                      strokeWidth={2.15}
                      isAnimationActive={false}
                      connectNulls
                      legendType="none"
                    />
                    <Line
                      type="monotone"
                      dataKey="downSavgol"
                      name="Down SG"
                      stroke="#be123c"
                      strokeOpacity={1}
                      strokeDasharray="6 3"
                      dot={false}
                      activeDot={false}
                      strokeWidth={2.15}
                      isAnimationActive={false}
                      connectNulls
                      legendType="none"
                    />
                  </>
                ) : null}
                {traderMarks.length > 0 ? (
                  <Scatter
                    name="Trader fills"
                    data={traderMarks.map((m) => ({
                      t: m.t,
                      pricePct: m.pricePct,
                      side: m.side,
                      outcome: m.outcome,
                      active:
                        highlightTime != null && Math.abs(m.t - highlightTime) <= 750,
                    }))}
                    dataKey="pricePct"
                    shape={(p: {
                      cx?: number
                      cy?: number
                      payload?: TraderMark & { active?: boolean }
                    }) => (
                      <TraderMarkShape cx={p.cx} cy={p.cy} payload={p.payload} />
                    )}
                    isAnimationActive={false}
                    legendType="none"
                  />
                ) : null}
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
      )}
    </div>
  )
}
