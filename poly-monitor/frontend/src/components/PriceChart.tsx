import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Line,
  ReferenceArea,
  ReferenceLine,
  ResponsiveContainer,
  Scatter,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { formatCents } from '../api'
import ChartCollapseButton from './ChartCollapseButton'
import ChartEnlargeButton from './ChartEnlargeButton'
import {
  useChartViewport,
  type TimeDomain,
  type YDomain,
} from './useChartViewport'

export type { TimeDomain }
export type BtcSeriesKey = 'twap' | 'chainlink' | 'binance'

export type BtcSeriesVisibility = Record<BtcSeriesKey, boolean>

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
  /** BTC series momentum ($/s): (v(t) − v(t−Δ)) / Δ */
  twapMom?: number
  chainlinkMom?: number
  btcMom?: number
  /** Outcomes momentum (¢/s) — raw / EMA / mom(SG price) / SG(mom) */
  upMom?: number
  downMom?: number
  upEmaMom?: number
  downEmaMom?: number
  upSavgolMom?: number
  downSavgolMom?: number
  /** Savitzky–Golay applied to each momentum series */
  upMomSg?: number
  downMomSg?: number
  upEmaMomSg?: number
  downEmaMomSg?: number
  upSavgolMomSg?: number
  downSavgolMomSg?: number
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
  /** Live mode: whether the shared X domain is following the trailing window */
  followLive?: boolean
  /** Show Follow live control (typically when live and not following) */
  showFollowLive?: boolean
  onFollowLive?: () => void
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
  /** Rendered inside the large-chart lightbox (taller plot, close control). */
  lightbox?: boolean
  onLightboxClose?: () => void
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
  twapMom?: number | null
  chainlinkMom?: number | null
  btcMom?: number | null
  upMom?: number | null
  downMom?: number | null
  upEmaMom?: number | null
  downEmaMom?: number | null
  upSavgolMom?: number | null
  downSavgolMom?: number | null
  upMomSg?: number | null
  downMomSg?: number | null
  upEmaMomSg?: number | null
  downEmaMomSg?: number | null
  upSavgolMomSg?: number | null
  downSavgolMomSg?: number | null
}

const DEFAULT_EMA_PERIOD = 20
/** Odd window length for Savitzky–Golay (samples). */
const DEFAULT_SAVGOL_WINDOW = 11
const DEFAULT_SAVGOL_POLY = 2
/** Lookback Δ for momentum velocity, matching momentum_pair default. */
const MOMENTUM_DELTA_MS = 1000
const MOMENTUM_COLOR = '#7c3aed'

function clampEmaPeriod(n: number): number {
  const v = Math.round(Number(n))
  if (!Number.isFinite(v)) return DEFAULT_EMA_PERIOD
  return Math.max(1, Math.min(120, v))
}

/** Savitzky–Golay window must be odd and ≥ polyorder + 1. */
function clampSavgolWindow(n: number, poly = DEFAULT_SAVGOL_POLY): number {
  let w = Math.round(Number(n))
  if (!Number.isFinite(w)) w = DEFAULT_SAVGOL_WINDOW
  w = Math.max(3, Math.min(101, w))
  if (w % 2 === 0) w += 1
  if (w > 101) w = 101
  const need = Math.max(3, Math.floor(poly) + 1)
  const needOdd = need % 2 === 0 ? need + 1 : need
  return Math.max(needOdd, w)
}

function clampSavgolPoly(n: number, window: number): number {
  let p = Math.round(Number(n))
  if (!Number.isFinite(p)) p = DEFAULT_SAVGOL_POLY
  return Math.max(0, Math.min(Math.max(0, window - 1), p))
}

type MomSourceKey = 'price' | 'ema' | 'sg'
type MomSideKey = 'up' | 'down'
type MomCurveKey = 'priceRaw' | 'priceSg' | 'emaRaw' | 'emaSg' | 'sgRaw' | 'sgSg'
type MomSourceFilter = Record<MomSourceKey, boolean>
type MomSideFilter = Record<MomSideKey, boolean>
type MomCurveFilter = Record<MomCurveKey, boolean>

const DEFAULT_MOM_SOURCE: MomSourceFilter = { price: true, ema: false, sg: false }
const DEFAULT_MOM_SIDE: MomSideFilter = { up: true, down: true }
/** Per-curve toggles on the momentum panel (raw′ vs SG(′)). */
const DEFAULT_MOM_CURVE: MomCurveFilter = {
  priceRaw: true,
  priceSg: true,
  emaRaw: true,
  emaSg: true,
  sgRaw: true,
  sgSg: true,
}

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

/**
 * First derivative / velocity: (v(t) − v(t−Δ)) / Δ_seconds.
 * Uses the latest finite sample at or before t−Δ (same rule as momentum_pair).
 */
function momentumSeries(
  times: number[],
  values: Array<number | null | undefined>,
  deltaMs = MOMENTUM_DELTA_MS,
): Array<number | undefined> {
  const out: Array<number | undefined> = new Array(values.length)
  for (let i = 0; i < values.length; i++) {
    const v = values[i]
    const t = times[i]
    if (v == null || !Number.isFinite(v) || !Number.isFinite(t)) {
      out[i] = undefined
      continue
    }
    const target = t - deltaMs
    let bestTs: number | null = null
    let bestVal: number | null = null
    for (let j = i - 1; j >= 0; j--) {
      const tj = times[j]
      const vj = values[j]
      if (vj == null || !Number.isFinite(vj) || !Number.isFinite(tj)) continue
      if (tj > target) continue
      // First hit walking backward is the latest sample at or before target.
      bestTs = tj
      bestVal = vj
      break
    }
    if (bestVal == null || bestTs == null) {
      out[i] = undefined
      continue
    }
    const dt = (t - bestTs) / 1000
    if (dt <= 0) {
      out[i] = undefined
      continue
    }
    out[i] = (v - bestVal) / dt
  }
  return out
}

function attachMomentum(
  rows: ChartPoint[],
  mode: 'btc' | 'outcomes',
  savgolWindow = DEFAULT_SAVGOL_WINDOW,
  savgolPoly = DEFAULT_SAVGOL_POLY,
): ChartPoint[] {
  if (!rows.length) return rows
  const times = rows.map((d) => Number(d.t))
  if (mode === 'btc') {
    const twapMom = momentumSeries(
      times,
      rows.map((d) => d.twap),
    )
    const chainlinkMom = momentumSeries(
      times,
      rows.map((d) => d.chainlink),
    )
    const btcMom = momentumSeries(
      times,
      rows.map((d) => d.btc),
    )
    return rows.map((d, i) => ({
      ...d,
      twapMom: twapMom[i],
      chainlinkMom: chainlinkMom[i],
      btcMom: btcMom[i],
    }))
  }
  const upMom = momentumSeries(
    times,
    rows.map((d) => d.upPct),
  )
  const downMom = momentumSeries(
    times,
    rows.map((d) => d.downPct),
  )
  const upEmaMom = momentumSeries(
    times,
    rows.map((d) => d.upEma),
  )
  const downEmaMom = momentumSeries(
    times,
    rows.map((d) => d.downEma),
  )
  const upSavgolMom = momentumSeries(
    times,
    rows.map((d) => d.upSavgol),
  )
  const downSavgolMom = momentumSeries(
    times,
    rows.map((d) => d.downSavgol),
  )
  // SG applied to each momentum series: SG(Price′), SG(EMA′), SG(SG′).
  const upMomSg = savgolSeries(upMom, savgolWindow, savgolPoly)
  const downMomSg = savgolSeries(downMom, savgolWindow, savgolPoly)
  const upEmaMomSg = savgolSeries(upEmaMom, savgolWindow, savgolPoly)
  const downEmaMomSg = savgolSeries(downEmaMom, savgolWindow, savgolPoly)
  const upSavgolMomSg = savgolSeries(upSavgolMom, savgolWindow, savgolPoly)
  const downSavgolMomSg = savgolSeries(downSavgolMom, savgolWindow, savgolPoly)
  return rows.map((d, i) => ({
    ...d,
    upMom: upMom[i],
    downMom: downMom[i],
    upEmaMom: upEmaMom[i],
    downEmaMom: downEmaMom[i],
    upSavgolMom: upSavgolMom[i],
    downSavgolMom: downSavgolMom[i],
    upMomSg: upMomSg[i],
    downMomSg: downMomSg[i],
    upEmaMomSg: upEmaMomSg[i],
    downEmaMomSg: downEmaMomSg[i],
    upSavgolMomSg: upSavgolMomSg[i],
    downSavgolMomSg: downSavgolMomSg[i],
  }))
}

function formatTipMomentum(mode: 'btc' | 'outcomes', raw: number): string {
  if (!Number.isFinite(raw)) return '—'
  const abs =
    mode === 'btc'
      ? Math.abs(raw).toLocaleString(undefined, {
          minimumFractionDigits: 2,
          maximumFractionDigits: 2,
        })
      : Math.abs(raw).toFixed(2)
  const sign = raw > 0 ? '+' : raw < 0 ? '−' : ''
  if (mode === 'btc') return `${sign}$${abs}/s`
  return `${sign}${abs}¢/s`
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
  showMomentum = false,
  momSource: MomSourceFilter = DEFAULT_MOM_SOURCE,
  momSide: MomSideFilter = DEFAULT_MOM_SIDE,
  momCurve: MomCurveFilter = DEFAULT_MOM_CURVE,
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
    if (showMomentum) {
      const momKeys: {
        key: BtcSeriesKey
        dataKey: 'twapMom' | 'chainlinkMom' | 'btcMom'
        label: string
        color: string
      }[] = [
        { key: 'twap', dataKey: 'twapMom', label: 'Current Mom', color: TWAP_COLOR },
        { key: 'chainlink', dataKey: 'chainlinkMom', label: 'Chainlink Mom', color: '#22c55e' },
        { key: 'binance', dataKey: 'btcMom', label: 'Binance Mom', color: '#2563eb' },
      ]
      for (const s of momKeys) {
        if (!seriesVisible[s.key]) continue
        const v = point[s.dataKey]
        if (v == null || !Number.isFinite(Number(v))) continue
        rows.push({
          label: s.label,
          color: s.color,
          dataKey: s.dataKey,
          valueText: formatTipMomentum('btc', Number(v)),
        })
      }
    }
  } else {
    const wantUp = !showMomentum || momSide.up
    const wantDown = !showMomentum || momSide.down
    const wantPrice = !showMomentum || momSource.price
    const wantEma = showMomentum ? momSource.ema : showEma
    const wantSg = showMomentum ? momSource.sg : showSavgol
    const showPriceRaw = !showMomentum || momCurve.priceRaw
    const showPriceSg = !showMomentum || momCurve.priceSg
    const showEmaRaw = !showMomentum || momCurve.emaRaw
    const showEmaSg = !showMomentum || momCurve.emaSg
    const showSgRaw = !showMomentum || momCurve.sgRaw
    const showSgSg = !showMomentum || momCurve.sgSg

    if (wantUp && wantPrice && point.upPct != null && Number.isFinite(point.upPct)) {
      rows.push({
        label: 'Up',
        color: '#10b981',
        dataKey: 'upPct',
        valueText: formatTipValue('outcomes', point.upPct),
      })
    }
    if (wantUp && wantEma && point.upEma != null && Number.isFinite(point.upEma)) {
      rows.push({
        label: 'Up EMA',
        color: '#059669',
        dataKey: 'upEma',
        valueText: formatTipValue('outcomes', point.upEma),
      })
    }
    if (wantUp && wantSg && point.upSavgol != null && Number.isFinite(point.upSavgol)) {
      rows.push({
        label: 'Up SG',
        color: '#0f766e',
        dataKey: 'upSavgol',
        valueText: formatTipValue('outcomes', point.upSavgol),
      })
    }
    if (
      showMomentum &&
      wantUp &&
      wantPrice &&
      showPriceRaw &&
      point.upMom != null &&
      Number.isFinite(point.upMom)
    ) {
      rows.push({
        label: 'Up′',
        color: '#10b981',
        dataKey: 'upMom',
        valueText: formatTipMomentum('outcomes', point.upMom),
      })
    }
    if (
      showMomentum &&
      wantUp &&
      wantPrice &&
      showPriceSg &&
      point.upMomSg != null &&
      Number.isFinite(point.upMomSg)
    ) {
      rows.push({
        label: 'SG(Up′)',
        color: '#0f766e',
        dataKey: 'upMomSg',
        valueText: formatTipMomentum('outcomes', point.upMomSg),
      })
    }
    if (
      showMomentum &&
      wantUp &&
      wantEma &&
      showEmaRaw &&
      point.upEmaMom != null &&
      Number.isFinite(point.upEmaMom)
    ) {
      rows.push({
        label: 'Up EMA′',
        color: '#059669',
        dataKey: 'upEmaMom',
        valueText: formatTipMomentum('outcomes', point.upEmaMom),
      })
    }
    if (
      showMomentum &&
      wantUp &&
      wantEma &&
      showEmaSg &&
      point.upEmaMomSg != null &&
      Number.isFinite(point.upEmaMomSg)
    ) {
      rows.push({
        label: 'SG(Up EMA′)',
        color: '#0f766e',
        dataKey: 'upEmaMomSg',
        valueText: formatTipMomentum('outcomes', point.upEmaMomSg),
      })
    }
    if (
      showMomentum &&
      wantUp &&
      wantSg &&
      showSgRaw &&
      point.upSavgolMom != null &&
      Number.isFinite(point.upSavgolMom)
    ) {
      rows.push({
        label: 'Up SG′',
        color: '#0f766e',
        dataKey: 'upSavgolMom',
        valueText: formatTipMomentum('outcomes', point.upSavgolMom),
      })
    }
    if (
      showMomentum &&
      wantUp &&
      wantSg &&
      showSgSg &&
      point.upSavgolMomSg != null &&
      Number.isFinite(point.upSavgolMomSg)
    ) {
      rows.push({
        label: 'SG(Up SG′)',
        color: '#115e59',
        dataKey: 'upSavgolMomSg',
        valueText: formatTipMomentum('outcomes', point.upSavgolMomSg),
      })
    }
    if (wantDown && wantPrice && point.downPct != null && Number.isFinite(point.downPct)) {
      rows.push({
        label: 'Down',
        color: '#ef4444',
        dataKey: 'downPct',
        valueText: formatTipValue('outcomes', point.downPct),
      })
    }
    if (wantDown && wantEma && point.downEma != null && Number.isFinite(point.downEma)) {
      rows.push({
        label: 'Down EMA',
        color: '#dc2626',
        dataKey: 'downEma',
        valueText: formatTipValue('outcomes', point.downEma),
      })
    }
    if (wantDown && wantSg && point.downSavgol != null && Number.isFinite(point.downSavgol)) {
      rows.push({
        label: 'Down SG',
        color: '#be123c',
        dataKey: 'downSavgol',
        valueText: formatTipValue('outcomes', point.downSavgol),
      })
    }
    if (
      showMomentum &&
      wantDown &&
      wantPrice &&
      showPriceRaw &&
      point.downMom != null &&
      Number.isFinite(point.downMom)
    ) {
      rows.push({
        label: 'Down′',
        color: '#ef4444',
        dataKey: 'downMom',
        valueText: formatTipMomentum('outcomes', point.downMom),
      })
    }
    if (
      showMomentum &&
      wantDown &&
      wantPrice &&
      showPriceSg &&
      point.downMomSg != null &&
      Number.isFinite(point.downMomSg)
    ) {
      rows.push({
        label: 'SG(Down′)',
        color: '#be123c',
        dataKey: 'downMomSg',
        valueText: formatTipMomentum('outcomes', point.downMomSg),
      })
    }
    if (
      showMomentum &&
      wantDown &&
      wantEma &&
      showEmaRaw &&
      point.downEmaMom != null &&
      Number.isFinite(point.downEmaMom)
    ) {
      rows.push({
        label: 'Down EMA′',
        color: '#dc2626',
        dataKey: 'downEmaMom',
        valueText: formatTipMomentum('outcomes', point.downEmaMom),
      })
    }
    if (
      showMomentum &&
      wantDown &&
      wantEma &&
      showEmaSg &&
      point.downEmaMomSg != null &&
      Number.isFinite(point.downEmaMomSg)
    ) {
      rows.push({
        label: 'SG(Down EMA′)',
        color: '#be123c',
        dataKey: 'downEmaMomSg',
        valueText: formatTipMomentum('outcomes', point.downEmaMomSg),
      })
    }
    if (
      showMomentum &&
      wantDown &&
      wantSg &&
      showSgRaw &&
      point.downSavgolMom != null &&
      Number.isFinite(point.downSavgolMom)
    ) {
      rows.push({
        label: 'Down SG′',
        color: '#be123c',
        dataKey: 'downSavgolMom',
        valueText: formatTipMomentum('outcomes', point.downSavgolMom),
      })
    }
    if (
      showMomentum &&
      wantDown &&
      wantSg &&
      showSgSg &&
      point.downSavgolMomSg != null &&
      Number.isFinite(point.downSavgolMomSg)
    ) {
      rows.push({
        label: 'SG(Down SG′)',
        color: '#9f1239',
        dataKey: 'downSavgolMomSg',
        valueText: formatTipMomentum('outcomes', point.downSavgolMomSg),
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

export default function PriceChart(props: Props) {
  const {
    data,
    priceToBeat,
    mode = 'btc',
    title,
    xDomain,
    onXDomainChange,
    onXDomainReset,
    xFullDomain,
    xDefaultDomain,
    followLive = true,
    showFollowLive = false,
    onFollowLive,
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
    emaPeriod: emaPeriodProp = DEFAULT_EMA_PERIOD,
    savgolWindow: savgolWindowProp = DEFAULT_SAVGOL_WINDOW,
    savgolPoly: savgolPolyProp = DEFAULT_SAVGOL_POLY,
    lightbox = false,
    onLightboxClose,
  } = props
  const showBtc = mode === 'btc'
  /** Lightbox: shared Price/EMA/SG + Up/Down filters drive price + momentum. */
  const showMomentum = lightbox
  const [momSource, setMomSource] = useState<MomSourceFilter>(DEFAULT_MOM_SOURCE)
  const [momSide, setMomSide] = useState<MomSideFilter>(DEFAULT_MOM_SIDE)
  const [momCurve, setMomCurve] = useState<MomCurveFilter>(DEFAULT_MOM_CURVE)
  const [emaPeriod, setEmaPeriod] = useState(() => clampEmaPeriod(emaPeriodProp))
  const [savgolWindow, setSavgolWindow] = useState(() =>
    clampSavgolWindow(savgolWindowProp, savgolPolyProp),
  )
  const [savgolPoly, setSavgolPoly] = useState(() =>
    clampSavgolPoly(savgolPolyProp, savgolWindowProp),
  )
  useEffect(() => {
    setEmaPeriod(clampEmaPeriod(emaPeriodProp))
  }, [emaPeriodProp])
  useEffect(() => {
    const w = clampSavgolWindow(savgolWindowProp, savgolPolyProp)
    const p = clampSavgolPoly(savgolPolyProp, w)
    setSavgolWindow(w)
    setSavgolPoly(p)
  }, [savgolWindowProp, savgolPolyProp])
  const plotPrice = lightbox ? momSource.price : true
  const plotEma = lightbox ? momSource.ema : showEma
  const plotSg = lightbox ? momSource.sg : showSavgol
  const plotUp = lightbox ? momSide.up : true
  const plotDown = lightbox ? momSide.down : true
  const showSmooth = plotEma || plotSg
  const twapFillId = `twapAreaFill-${mode}${lightbox ? '-lb' : ''}`
  const [localHoverTime, setLocalHoverTime] = useState<number | null>(null)
  const [collapsed, setCollapsed] = useState(false)
  const [enlarged, setEnlarged] = useState(false)
  const chartTitle = title || (showBtc ? 'BTC price' : 'Up / Down price')
  const hoverTime = onHoverTimeChange
    ? (highlightTime ?? hoverTimeProp ?? null)
    : (highlightTime ?? localHoverTime)
  const setHoverTime = onHoverTimeChange ?? setLocalHoverTime

  const chartData = useMemo((): ChartPoint[] => {
    const base: ChartPoint[] =
      mode !== 'outcomes'
        ? data.map((d) => ({
            ...d,
            upPct: undefined,
            downPct: undefined,
          }))
        : mapOutcomesChartData(data, emaPeriod, savgolWindow, savgolPoly)
    if (!lightbox) return base
    return attachMomentum(
      base,
      mode === 'outcomes' ? 'outcomes' : 'btc',
      savgolWindow,
      savgolPoly,
    )
  }, [data, mode, emaPeriod, savgolWindow, savgolPoly, lightbox])

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
      showMomentum,
      momSource,
      momSide,
      momCurve,
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
    showMomentum,
    momSource,
    momSide,
    momCurve,
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

  const autoY = useMemo((): YDomain => {
    if (!showBtc) return [0, 100]
    const [x0, x1] = xDomain
    const values = chartData.flatMap((d) => {
      const t = Number(d.t)
      if (!Number.isFinite(t) || t < x0 || t > x1) return []
      return SERIES_META.filter((s) => plotVisible[s.key])
        .map((s) => d[s.dataKey])
        .filter((v): v is number => v != null && Number.isFinite(v))
    })
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
    xDomain,
    plotVisible.twap,
    plotVisible.chainlink,
    plotVisible.binance,
  ])

  const momSeriesKeys = useMemo((): Array<keyof ChartPoint> => {
    if (!showMomentum) return []
    if (showBtc) {
      return [
        plotVisible.twap ? ('twapMom' as const) : null,
        plotVisible.chainlink ? ('chainlinkMom' as const) : null,
        plotVisible.binance ? ('btcMom' as const) : null,
      ].filter(Boolean) as Array<keyof ChartPoint>
    }
    return [
      // Price′ and SG(Price′)
      momSide.up && momSource.price && momCurve.priceRaw ? ('upMom' as const) : null,
      momSide.down && momSource.price && momCurve.priceRaw ? ('downMom' as const) : null,
      momSide.up && momSource.price && momCurve.priceSg ? ('upMomSg' as const) : null,
      momSide.down && momSource.price && momCurve.priceSg ? ('downMomSg' as const) : null,
      // EMA′ and SG(EMA′)
      momSide.up && momSource.ema && momCurve.emaRaw ? ('upEmaMom' as const) : null,
      momSide.down && momSource.ema && momCurve.emaRaw ? ('downEmaMom' as const) : null,
      momSide.up && momSource.ema && momCurve.emaSg ? ('upEmaMomSg' as const) : null,
      momSide.down && momSource.ema && momCurve.emaSg ? ('downEmaMomSg' as const) : null,
      // SG′ = mom(SG price) and SG(SG′)
      momSide.up && momSource.sg && momCurve.sgRaw ? ('upSavgolMom' as const) : null,
      momSide.down && momSource.sg && momCurve.sgRaw ? ('downSavgolMom' as const) : null,
      momSide.up && momSource.sg && momCurve.sgSg ? ('upSavgolMomSg' as const) : null,
      momSide.down && momSource.sg && momCurve.sgSg ? ('downSavgolMomSg' as const) : null,
    ].filter(Boolean) as Array<keyof ChartPoint>
  }, [
    showMomentum,
    showBtc,
    plotVisible.twap,
    plotVisible.chainlink,
    plotVisible.binance,
    momSource,
    momSide,
    momCurve,
  ])

  const momYDomain = useMemo((): [number, number] => {
    if (!showMomentum) return [-1, 1]
    const values: number[] = []
    for (const d of chartData) {
      const t = Number(d.t)
      if (!Number.isFinite(t) || t < xDomain[0] || t > xDomain[1]) continue
      for (const k of momSeriesKeys) {
        const v = d[k]
        if (typeof v === 'number' && Number.isFinite(v)) values.push(v)
      }
    }
    if (!values.length) return [-1, 1]
    const peak = Math.max(...values.map(Math.abs), 1e-9)
    const pad = peak * 0.15
    return [-(peak + pad), peak + pad]
  }, [showMomentum, chartData, xDomain, momSeriesKeys])

  /** Split momentum into +/− for green/red area fills under the curves. */
  const momChartData = useMemo(() => {
    if (!showMomentum || !momSeriesKeys.length) return chartData
    return chartData.map((d) => {
      const row: Record<string, unknown> = { ...d }
      for (const k of momSeriesKeys) {
        const v = d[k]
        if (typeof v === 'number' && Number.isFinite(v)) {
          row[`${String(k)}_pos`] = v > 0 ? v : 0
          row[`${String(k)}_neg`] = v < 0 ? v : 0
        } else {
          row[`${String(k)}_pos`] = undefined
          row[`${String(k)}_neg`] = undefined
        }
      }
      return row
    })
  }, [showMomentum, chartData, momSeriesKeys])

  // null = follow autoY; set when user zooms/pans vertically
  const [yZoom, setYZoom] = useState<YDomain | null>(null)
  const yDomain = yZoom ?? autoY
  const chartMargin = CHART_MARGIN

  const clampY = useCallback(
    (d: YDomain): YDomain => {
      if (showBtc) return d
      return [Math.max(0, d[0]), Math.min(100, d[1])]
    },
    [showBtc],
  )

  const { hoverZone, canReset, resetZoom, bind: viewportBind } = useChartViewport({
    xDomain,
    xFullDomain,
    xDefaultDomain,
    onXDomainChange,
    onXDomainReset,
    yDomain,
    setYDomain: setYZoom,
    clampY,
    minYSpan: showBtc ? 1 : 0.5,
    margin: chartMargin,
    yAxisWidth: Y_AXIS_WIDTH,
    enableY: true,
    yManual: yZoom != null,
  })

  const lastTwap =
    [...chartData].reverse().find((d) => d.twap != null)?.twap ??
    [...chartData].reverse().find((d) => d.btc != null)?.btc ??
    null
  const aboveTarget =
    lastTwap != null && priceToBeat != null ? lastTwap >= priceToBeat : null

  // Keep manual Y zoom across live ticks; only reset when the market window changes.
  const yResetKey = `${mode}:${xDefaultDomain[0]}:${xDefaultDomain[1]}`
  useEffect(() => {
    setYZoom(null)
  }, [yResetKey])

  const followLiveAction = () => {
    if (onFollowLive) onFollowLive()
    else resetZoom()
  }

  const toggle = (key: BtcSeriesKey) => {
    if (!onSeriesVisibleChange) return
    const next = { ...seriesVisible, [key]: !seriesVisible[key] }
    if (!next.twap && !next.chainlink && !next.binance) return
    onSeriesVisibleChange(next)
  }

  const toggleMomSource = (key: MomSourceKey) => {
    setMomSource((prev) => {
      const next = { ...prev, [key]: !prev[key] }
      if (!next.price && !next.ema && !next.sg) return prev
      return next
    })
  }

  const toggleMomSide = (key: MomSideKey) => {
    setMomSide((prev) => {
      const next = { ...prev, [key]: !prev[key] }
      if (!next.up && !next.down) return prev
      return next
    })
  }

  const toggleMomCurve = (key: MomCurveKey) => {
    setMomCurve((prev) => ({ ...prev, [key]: !prev[key] }))
  }

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

  return (
    <>
    <div
      className={`chart-block${collapsed && !lightbox ? ' chart-block-collapsed' : ''}${
        lightbox ? ' chart-block-lightbox' : ''
      }`}
    >
      {lightbox && !showBtc ? (
        <div className="chart-lightbox-filters">
          <div className="chart-series-toggles" role="group" aria-label="Series source">
            {(
              [
                { key: 'price' as const, label: 'Price', color: '#10b981' },
                { key: 'ema' as const, label: 'EMA', color: '#059669' },
                { key: 'sg' as const, label: 'SG', color: '#0f766e' },
              ] as const
            ).map((s) => (
              <label
                key={s.key}
                className={`chart-series-toggle ${momSource[s.key] ? 'on' : ''}`}
              >
                <input
                  type="checkbox"
                  checked={momSource[s.key]}
                  onChange={() => toggleMomSource(s.key)}
                />
                <span className="chart-series-swatch" style={{ background: s.color }} />
                {s.label}
              </label>
            ))}
          </div>
          <div className="chart-series-toggles" role="group" aria-label="Outcome side">
            <label className={`chart-series-toggle ${momSide.up ? 'on' : ''}`}>
              <input
                type="checkbox"
                checked={momSide.up}
                onChange={() => toggleMomSide('up')}
              />
              <span className="chart-series-swatch" style={{ background: '#10b981' }} />
              Up
            </label>
            <label className={`chart-series-toggle ${momSide.down ? 'on' : ''}`}>
              <input
                type="checkbox"
                checked={momSide.down}
                onChange={() => toggleMomSide('down')}
              />
              <span className="chart-series-swatch" style={{ background: '#ef4444' }} />
              Down
            </label>
          </div>
          <div className="chart-filter-params" role="group" aria-label="Filter parameters">
            <label
              className={`chart-filter-param${momSource.ema ? ' on' : ''}`}
              title="EMA period (samples)"
            >
              <span>EMA</span>
              <input
                type="number"
                min={1}
                max={120}
                step={1}
                value={emaPeriod}
                disabled={!momSource.ema}
                onChange={(ev) => setEmaPeriod(clampEmaPeriod(Number(ev.target.value)))}
              />
            </label>
            <label
              className={`chart-filter-param${momSource.sg ? ' on' : ''}`}
              title="Savitzky–Golay window (odd samples)"
            >
              <span>SG win</span>
              <input
                type="number"
                min={3}
                max={101}
                step={2}
                value={savgolWindow}
                disabled={!momSource.sg}
                onChange={(ev) => {
                  const w = clampSavgolWindow(Number(ev.target.value), savgolPoly)
                  setSavgolWindow(w)
                  setSavgolPoly((p) => clampSavgolPoly(p, w))
                }}
              />
            </label>
            <label
              className={`chart-filter-param${momSource.sg ? ' on' : ''}`}
              title="Savitzky–Golay polynomial order"
            >
              <span>SG poly</span>
              <input
                type="number"
                min={0}
                max={Math.max(0, savgolWindow - 1)}
                step={1}
                value={savgolPoly}
                disabled={!momSource.sg}
                onChange={(ev) => {
                  const p = clampSavgolPoly(Number(ev.target.value), savgolWindow)
                  setSavgolPoly(p)
                  setSavgolWindow((w) => clampSavgolWindow(w, p))
                }}
              />
            </label>
          </div>
        </div>
      ) : null}
      <div className="chart-header">
        <div className="chart-header-left">
          <div className="chart-title-row">
            {!lightbox ? (
              <ChartCollapseButton
                collapsed={collapsed}
                onToggle={() => setCollapsed((v) => !v)}
                label={chartTitle}
              />
            ) : null}
            {title && <div className="chart-title">{title}</div>}
            {!collapsed || lightbox ? (
              <ChartEnlargeButton
                label={chartTitle}
                mode={lightbox ? 'close' : 'enlarge'}
                onClick={() => {
                  if (lightbox) onLightboxClose?.()
                  else setEnlarged(true)
                }}
              />
            ) : null}
          </div>
          {(!collapsed || lightbox) && <ChartHeaderTip tip={hoverTip} />}
        </div>
        <div className="chart-header-right">
          {(!collapsed || lightbox) && showBtc && onSeriesVisibleChange ? (
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
          {!lightbox &&
          !collapsed &&
          !showBtc &&
          (onShowEmaChange || onShowSavgolChange) ? (
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
      {(!collapsed || lightbox) && (
      <>
      <div
        className={`chart-wrap chart-wrap-zoom chart-cursor-${hoverZone}${
          lightbox ? ' chart-wrap-lightbox' : ''
        }${showMomentum ? ' chart-wrap-main-with-mom' : ''}`}
        {...viewportBind}
        onPointerLeave={() => {
          viewportBind.onPointerLeave()
          clearHover()
        }}
      >
        <div className="chart-zoom-zone chart-zoom-zone-time" aria-hidden />
        <div className="chart-zoom-zone chart-zoom-zone-price" aria-hidden />
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart
            data={chartData}
            margin={chartMargin}
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
                  yAxisId="price"
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
                    yAxisId="price"
                    x={highlightTime}
                    stroke="#3b82f6"
                    strokeWidth={1.75}
                    strokeDasharray="4 3"
                    ifOverflow="hidden"
                  />
                )}
                {priceToBeat != null && (
                  <ReferenceLine
                    yAxisId="price"
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
                      yAxisId="price"
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
                      yAxisId="price"
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
                      yAxisId="price"
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
                  yAxisId="price"
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
                    yAxisId="price"
                    x={highlightTime}
                    stroke="#3b82f6"
                    strokeWidth={1.75}
                    strokeDasharray="4 3"
                    ifOverflow="hidden"
                  />
                )}
                {plotUp && plotPrice ? (
                  <Line
                    yAxisId="price"
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
                ) : null}
                {plotDown && plotPrice ? (
                  <Line
                    yAxisId="price"
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
                ) : null}
                {plotEma ? (
                  <>
                    {plotUp ? (
                      <Line
                        yAxisId="price"
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
                    ) : null}
                    {plotDown ? (
                      <Line
                        yAxisId="price"
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
                    ) : null}
                  </>
                ) : null}
                {plotSg ? (
                  <>
                    {plotUp ? (
                      <Line
                        yAxisId="price"
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
                    ) : null}
                    {plotDown ? (
                      <Line
                        yAxisId="price"
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
                    ) : null}
                  </>
                ) : null}
                {traderMarks.length > 0 ? (
                  <Scatter
                    yAxisId="price"
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
        {(canReset || showFollowLive) && (
          <div className="chart-viewport-controls">
            {showFollowLive && (
              <button
                type="button"
                className={`chart-follow-btn${!followLive ? ' active' : ''}`}
                onClick={(e) => {
                  e.stopPropagation()
                  followLiveAction()
                }}
                onPointerDown={(e) => e.stopPropagation()}
                aria-label="Follow live"
                title="Follow live"
              >
                Follow live
              </button>
            )}
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
      {showMomentum ? (
        <div className="chart-momentum-panel">
          <div className="chart-momentum-head">
            <div className="chart-title">Momentum</div>
            {!showBtc ? (
              <div className="chart-series-toggles" role="group" aria-label="Momentum series">
                {momSource.price ? (
                  <>
                    <label
                      className={`chart-series-toggle ${momCurve.priceRaw ? 'on' : ''}`}
                    >
                      <input
                        type="checkbox"
                        checked={momCurve.priceRaw}
                        onChange={() => toggleMomCurve('priceRaw')}
                      />
                      <span className="chart-series-swatch" style={{ background: '#10b981' }} />
                      Price′
                    </label>
                    <label
                      className={`chart-series-toggle ${momCurve.priceSg ? 'on' : ''}`}
                    >
                      <input
                        type="checkbox"
                        checked={momCurve.priceSg}
                        onChange={() => toggleMomCurve('priceSg')}
                      />
                      <span className="chart-series-swatch" style={{ background: '#0f766e' }} />
                      SG(Price′)
                    </label>
                  </>
                ) : null}
                {momSource.ema ? (
                  <>
                    <label className={`chart-series-toggle ${momCurve.emaRaw ? 'on' : ''}`}>
                      <input
                        type="checkbox"
                        checked={momCurve.emaRaw}
                        onChange={() => toggleMomCurve('emaRaw')}
                      />
                      <span className="chart-series-swatch" style={{ background: '#059669' }} />
                      EMA′
                    </label>
                    <label className={`chart-series-toggle ${momCurve.emaSg ? 'on' : ''}`}>
                      <input
                        type="checkbox"
                        checked={momCurve.emaSg}
                        onChange={() => toggleMomCurve('emaSg')}
                      />
                      <span className="chart-series-swatch" style={{ background: '#0f766e' }} />
                      SG(EMA′)
                    </label>
                  </>
                ) : null}
                {momSource.sg ? (
                  <>
                    <label className={`chart-series-toggle ${momCurve.sgRaw ? 'on' : ''}`}>
                      <input
                        type="checkbox"
                        checked={momCurve.sgRaw}
                        onChange={() => toggleMomCurve('sgRaw')}
                      />
                      <span className="chart-series-swatch" style={{ background: '#0f766e' }} />
                      SG′
                    </label>
                    <label className={`chart-series-toggle ${momCurve.sgSg ? 'on' : ''}`}>
                      <input
                        type="checkbox"
                        checked={momCurve.sgSg}
                        onChange={() => toggleMomCurve('sgSg')}
                      />
                      <span className="chart-series-swatch" style={{ background: '#115e59' }} />
                      SG(SG′)
                    </label>
                  </>
                ) : null}
              </div>
            ) : null}
            <div className="chart-momentum-sign-legend" aria-hidden>
              <span className="chart-momentum-sign pos">+ positive</span>
              <span className="chart-momentum-sign neg">− negative</span>
            </div>
          </div>
          <div className="chart-wrap chart-wrap-momentum">
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart
                data={momChartData}
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
                <YAxis
                  orientation="right"
                  domain={momYDomain}
                  allowDataOverflow
                  stroke={MOMENTUM_COLOR}
                  tick={{ fontSize: 10, fill: MOMENTUM_COLOR }}
                  width={Y_AXIS_WIDTH}
                  axisLine={false}
                  tickLine={false}
                  tickFormatter={(v) => {
                    const n = Number(v)
                    if (showBtc) {
                      const abs = Math.abs(n)
                      const t =
                        abs >= 10 ? n.toFixed(0) : abs >= 1 ? n.toFixed(1) : n.toFixed(2)
                      return `${t}/s`
                    }
                    return `${n.toFixed(2)}¢/s`
                  }}
                />
                <Tooltip
                  content={() => null}
                  cursor={<ChartCrosshair />}
                  isAnimationActive={false}
                />
                <ReferenceArea
                  y1={0}
                  y2={momYDomain[1]}
                  fill="#10b981"
                  fillOpacity={0.07}
                  ifOverflow="hidden"
                />
                <ReferenceArea
                  y1={momYDomain[0]}
                  y2={0}
                  fill="#ef4444"
                  fillOpacity={0.07}
                  ifOverflow="hidden"
                />
                <ReferenceLine
                  y={0}
                  stroke="#64748b"
                  strokeOpacity={0.85}
                  strokeWidth={1.5}
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
                {momSeriesKeys.map((key) => (
                  <Area
                    key={`${String(key)}_pos`}
                    type="monotone"
                    dataKey={`${String(key)}_pos`}
                    stroke="none"
                    fill="#10b981"
                    fillOpacity={0.22}
                    baseValue={0}
                    isAnimationActive={false}
                    connectNulls
                    legendType="none"
                    tooltipType="none"
                  />
                ))}
                {momSeriesKeys.map((key) => (
                  <Area
                    key={`${String(key)}_neg`}
                    type="monotone"
                    dataKey={`${String(key)}_neg`}
                    stroke="none"
                    fill="#ef4444"
                    fillOpacity={0.22}
                    baseValue={0}
                    isAnimationActive={false}
                    connectNulls
                    legendType="none"
                    tooltipType="none"
                  />
                ))}
                {showBtc ? (
                  (
                    [
                      {
                        key: 'twap' as const,
                        dataKey: 'twapMom',
                        color: TWAP_COLOR,
                        label: 'Current′',
                      },
                      {
                        key: 'chainlink' as const,
                        dataKey: 'chainlinkMom',
                        color: '#22c55e',
                        label: 'Chainlink′',
                      },
                      {
                        key: 'binance' as const,
                        dataKey: 'btcMom',
                        color: '#2563eb',
                        label: 'Binance′',
                      },
                    ] as const
                  ).map((s) =>
                    plotVisible[s.key] ? (
                      <Line
                        key={s.dataKey}
                        type="monotone"
                        dataKey={s.dataKey}
                        name={s.label}
                        stroke={s.color}
                        strokeWidth={1.85}
                        strokeDasharray="5 3"
                        dot={false}
                        activeDot={false}
                        isAnimationActive={false}
                        connectNulls
                      />
                    ) : null,
                  )
                ) : (
                  <>
                    {momSide.up && momSource.price && momCurve.priceRaw ? (
                      <Line
                        type="monotone"
                        dataKey="upMom"
                        name="Up′"
                        stroke="#10b981"
                        strokeWidth={1.5}
                        strokeOpacity={0.45}
                        strokeDasharray="3 3"
                        dot={false}
                        activeDot={false}
                        isAnimationActive={false}
                        connectNulls
                      />
                    ) : null}
                    {momSide.down && momSource.price && momCurve.priceRaw ? (
                      <Line
                        type="monotone"
                        dataKey="downMom"
                        name="Down′"
                        stroke="#ef4444"
                        strokeWidth={1.5}
                        strokeOpacity={0.45}
                        strokeDasharray="3 3"
                        dot={false}
                        activeDot={false}
                        isAnimationActive={false}
                        connectNulls
                      />
                    ) : null}
                    {momSide.up && momSource.price && momCurve.priceSg ? (
                      <Line
                        type="monotone"
                        dataKey="upMomSg"
                        name="SG(Up′)"
                        stroke="#0f766e"
                        strokeWidth={2.35}
                        strokeDasharray="6 3"
                        dot={false}
                        activeDot={false}
                        isAnimationActive={false}
                        connectNulls
                      />
                    ) : null}
                    {momSide.down && momSource.price && momCurve.priceSg ? (
                      <Line
                        type="monotone"
                        dataKey="downMomSg"
                        name="SG(Down′)"
                        stroke="#be123c"
                        strokeWidth={2.35}
                        strokeDasharray="6 3"
                        dot={false}
                        activeDot={false}
                        isAnimationActive={false}
                        connectNulls
                      />
                    ) : null}
                    {momSide.up && momSource.ema && momCurve.emaRaw ? (
                      <Line
                        type="monotone"
                        dataKey="upEmaMom"
                        name="Up EMA′"
                        stroke="#059669"
                        strokeWidth={1.5}
                        strokeOpacity={0.45}
                        strokeDasharray="3 3"
                        dot={false}
                        activeDot={false}
                        isAnimationActive={false}
                        connectNulls
                      />
                    ) : null}
                    {momSide.down && momSource.ema && momCurve.emaRaw ? (
                      <Line
                        type="monotone"
                        dataKey="downEmaMom"
                        name="Down EMA′"
                        stroke="#dc2626"
                        strokeWidth={1.5}
                        strokeOpacity={0.45}
                        strokeDasharray="3 3"
                        dot={false}
                        activeDot={false}
                        isAnimationActive={false}
                        connectNulls
                      />
                    ) : null}
                    {momSide.up && momSource.ema && momCurve.emaSg ? (
                      <Line
                        type="monotone"
                        dataKey="upEmaMomSg"
                        name="SG(Up EMA′)"
                        stroke="#0f766e"
                        strokeWidth={2.2}
                        strokeDasharray="4 2"
                        dot={false}
                        activeDot={false}
                        isAnimationActive={false}
                        connectNulls
                      />
                    ) : null}
                    {momSide.down && momSource.ema && momCurve.emaSg ? (
                      <Line
                        type="monotone"
                        dataKey="downEmaMomSg"
                        name="SG(Down EMA′)"
                        stroke="#be123c"
                        strokeWidth={2.2}
                        strokeDasharray="4 2"
                        dot={false}
                        activeDot={false}
                        isAnimationActive={false}
                        connectNulls
                      />
                    ) : null}
                    {momSide.up && momSource.sg && momCurve.sgRaw ? (
                      <Line
                        type="monotone"
                        dataKey="upSavgolMom"
                        name="Up SG′"
                        stroke="#0f766e"
                        strokeWidth={1.5}
                        strokeOpacity={0.45}
                        strokeDasharray="3 3"
                        dot={false}
                        activeDot={false}
                        isAnimationActive={false}
                        connectNulls
                      />
                    ) : null}
                    {momSide.down && momSource.sg && momCurve.sgRaw ? (
                      <Line
                        type="monotone"
                        dataKey="downSavgolMom"
                        name="Down SG′"
                        stroke="#be123c"
                        strokeWidth={1.5}
                        strokeOpacity={0.45}
                        strokeDasharray="3 3"
                        dot={false}
                        activeDot={false}
                        isAnimationActive={false}
                        connectNulls
                      />
                    ) : null}
                    {momSide.up && momSource.sg && momCurve.sgSg ? (
                      <Line
                        type="monotone"
                        dataKey="upSavgolMomSg"
                        name="SG(Up SG′)"
                        stroke="#115e59"
                        strokeWidth={2.35}
                        strokeDasharray="2 3"
                        dot={false}
                        activeDot={false}
                        isAnimationActive={false}
                        connectNulls
                      />
                    ) : null}
                    {momSide.down && momSource.sg && momCurve.sgSg ? (
                      <Line
                        type="monotone"
                        dataKey="downSavgolMomSg"
                        name="SG(Down SG′)"
                        stroke="#9f1239"
                        strokeWidth={2.35}
                        strokeDasharray="2 3"
                        dot={false}
                        activeDot={false}
                        isAnimationActive={false}
                        connectNulls
                      />
                    ) : null}
                  </>
                )}
              </ComposedChart>
            </ResponsiveContainer>
          </div>
        </div>
      ) : null}
      </>
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
              aria-label={`Large ${chartTitle}`}
              onClick={(ev) => ev.stopPropagation()}
            >
              <PriceChart
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
