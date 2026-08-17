import { useId, useMemo } from 'react'
import {
  Area,
  AreaChart,
  CartesianGrid,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { formatCentsTrade } from '../api'
import type { DirectionPrediction, PredictionDistribution } from '../api'

type Props = {
  distribution?: PredictionDistribution | null
  distributions?: PredictionDistribution[] | null
  predictions?: DirectionPrediction[] | null
  title?: string
}

const CHART_MARGIN = { top: 10, right: 12, left: 4, bottom: 28 }
const Y_AXIS_WIDTH = 44
const X_DOMAIN: [number, number] = [0, 1]
const X_TICKS = [0, 0.25, 0.5, 0.75, 1]
const GRID_N = 101

const UP_COLOR = '#3b82f6'
const DOWN_COLOR = '#ef4444'
const MUTED_UP = '#93c5fd'
const MUTED_DOWN = '#fca5a5'

function formatPriceTick(v: number): string {
  return `${Math.round(v * 100)}¢`
}

function signalColor(direction: 'UP' | 'DOWN' | null | undefined, muted = false): string {
  const up = direction !== 'DOWN'
  if (muted) return up ? MUTED_UP : MUTED_DOWN
  return up ? UP_COLOR : DOWN_COLOR
}

function interpolateDensity(
  pdf: { x: number; density: number }[],
  x: number,
): number {
  if (!pdf.length) return 0
  if (x <= pdf[0].x) return Math.max(0, pdf[0].density)
  if (x >= pdf[pdf.length - 1].x) return Math.max(0, pdf[pdf.length - 1].density)
  for (let i = 1; i < pdf.length; i++) {
    const a = pdf[i - 1]
    const b = pdf[i]
    if (x <= b.x) {
      const t = (x - a.x) / Math.max(1e-12, b.x - a.x)
      return Math.max(0, a.density + t * (b.density - a.density))
    }
  }
  return 0
}

function horizonKey(seconds: number): string {
  return `h${String(seconds).replace('.', 'p')}`
}

export default function PredictionDistChart({
  distribution = null,
  distributions = null,
  predictions = null,
  title = 'Prediction · Up price density',
}: Props) {
  const gradId = useId().replace(/:/g, '')

  const series = useMemo(() => {
    const list =
      distributions && distributions.length
        ? distributions
        : distribution
          ? [distribution]
          : []
    return [...list].sort((a, b) => a.horizon_seconds - b.horizon_seconds)
  }, [distribution, distributions])

  const current = series[0]?.current_up ?? null
  const isBeta = series.some((d) => d.family === 'beta')

  const directionByHorizon = useMemo(() => {
    const map = new Map<number, 'UP' | 'DOWN'>()
    for (const p of predictions ?? []) {
      map.set(p.horizon_seconds, p.direction)
    }
    for (const d of series) {
      if (!map.has(d.horizon_seconds) && current != null && Number.isFinite(d.mean)) {
        map.set(d.horizon_seconds, d.mean >= current ? 'UP' : 'DOWN')
      }
    }
    return map
  }, [predictions, series, current])

  const plot = useMemo(() => {
    if (!series.length) return []
    const cleaned = series.map((d) => ({
      horizon: d.horizon_seconds,
      key: horizonKey(d.horizon_seconds),
      pdf: (d.pdf ?? [])
        .filter((p) => Number.isFinite(p.x) && Number.isFinite(p.density))
        .map((p) => ({ x: p.x, density: Math.max(0, p.density) }))
        .sort((a, b) => a.x - b.x),
    }))

    const rows: Record<string, number>[] = []
    for (let i = 0; i < GRID_N; i++) {
      const x = i / (GRID_N - 1)
      const row: Record<string, number> = { x }
      for (const item of cleaned) {
        row[item.key] = interpolateDensity(item.pdf, x)
      }
      rows.push(row)
    }
    return rows
  }, [series])

  const familyLabel = isBeta ? 'Beta PDF' : 'Normal PDF'

  return (
    <div className="prediction-dist-chart">
      <div className="chart-title-row">
        <div className="chart-title">{title}</div>
        <div className="prediction-dist-legend muted">
          {series.length ? series.map((d) => `${d.horizon_seconds}s`).join(' + ') : '—'} · {familyLabel}
        </div>
      </div>

      <div className="prediction-dist-stats">
        {series.length === 0 ? (
          <div>
            <span>Mean μ</span>
            <strong>—</strong>
          </div>
        ) : (
          series.map((d) => {
            const dir = directionByHorizon.get(d.horizon_seconds) ?? null
            return (
              <div key={d.horizon_seconds}>
                <span style={{ color: signalColor(dir) }}>{d.horizon_seconds}s μ</span>
                <strong>{formatCentsTrade(d.mean)}</strong>
              </div>
            )
          })
        )}
      </div>

      <div className="prediction-dist-plot">
        {plot.length === 0 ? (
          <div className="prediction-prob-empty muted">Waiting for distribution…</div>
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={plot} margin={CHART_MARGIN}>
              <defs>
                {series.map((d, idx) => {
                  const dir = directionByHorizon.get(d.horizon_seconds) ?? null
                  const color = signalColor(dir, idx > 0)
                  const id = `${gradId}-${horizonKey(d.horizon_seconds)}`
                  return (
                    <linearGradient key={id} id={id} x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor={color} stopOpacity={0.35} />
                      <stop offset="100%" stopColor={color} stopOpacity={0.02} />
                    </linearGradient>
                  )
                })}
              </defs>
              <CartesianGrid strokeDasharray="3 3" stroke="#e4e6ed" vertical={false} />
              <XAxis
                dataKey="x"
                type="number"
                domain={X_DOMAIN}
                ticks={X_TICKS}
                allowDataOverflow
                tickFormatter={formatPriceTick}
                tick={{ fill: '#6b6f7b', fontSize: 11 }}
                minTickGap={20}
              />
              <YAxis
                width={Y_AXIS_WIDTH}
                tick={{ fill: '#6b6f7b', fontSize: 11 }}
                tickFormatter={(v) => (Number(v) >= 10 ? Number(v).toFixed(0) : Number(v).toFixed(1))}
              />
              <Tooltip
                formatter={(value, name) => {
                  const n = typeof value === 'number' ? value : Number(value)
                  const label = String(name).replace(/^h/, '').replace('p', '.') + 's'
                  return [Number.isFinite(n) ? n.toFixed(2) : '—', label]
                }}
                labelFormatter={(label) => `Up ${formatPriceTick(Number(label))}`}
                contentStyle={{
                  borderRadius: 10,
                  border: '1px solid #e4e6ed',
                  boxShadow: '0 4px 16px rgba(15,17,23,0.08)',
                }}
              />
              {current != null && Number.isFinite(current) && (
                <ReferenceLine
                  x={current}
                  stroke="#0f172a"
                  strokeWidth={2}
                  strokeDasharray="5 4"
                  label={{ value: 'now', position: 'insideTop', fill: '#0f172a', fontSize: 11 }}
                />
              )}
              {series.map((d, idx) => {
                const dir = directionByHorizon.get(d.horizon_seconds) ?? null
                const color = signalColor(dir, idx > 0)
                const key = horizonKey(d.horizon_seconds)
                return (
                  <ReferenceLine
                    key={`mu-${key}`}
                    x={d.mean}
                    stroke={color}
                    strokeWidth={idx === 0 ? 2 : 1.5}
                    strokeDasharray={idx === 0 ? undefined : '4 3'}
                    label={{
                      value: `${d.horizon_seconds}s`,
                      position: idx === 0 ? 'insideTopRight' : 'insideTopLeft',
                      fill: color,
                      fontSize: 11,
                    }}
                  />
                )
              })}
              {series.map((d, idx) => {
                const dir = directionByHorizon.get(d.horizon_seconds) ?? null
                const color = signalColor(dir, idx > 0)
                const key = horizonKey(d.horizon_seconds)
                return (
                  <Area
                    key={key}
                    type="monotone"
                    dataKey={key}
                    name={key}
                    stroke={color}
                    fill={`url(#${gradId}-${key})`}
                    strokeWidth={idx === 0 ? 2.5 : 2}
                    strokeDasharray={idx === 0 ? undefined : '6 4'}
                    fillOpacity={1}
                    isAnimationActive={false}
                    baseValue={0}
                  />
                )
              })}
            </AreaChart>
          </ResponsiveContainer>
        )}
      </div>
    </div>
  )
}
