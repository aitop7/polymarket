import { useMemo } from 'react'
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import type { TimeDomain } from './PriceChart'

export type PredictionPoint = {
  t: number
  p_up_3s?: number | null
  p_up_5s?: number | null
}

type Props = {
  data: PredictionPoint[]
  xDomain: TimeDomain
  hoverTime?: number | null
  onHoverTimeChange?: (t: number | null) => void
  title?: string
}

const Y_AXIS_WIDTH = 72
const CHART_MARGIN = { top: 10, right: 8, left: 4, bottom: 36 }

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

function formatPctTick(v: number): string {
  return `${Math.round(v * 100)}%`
}

export default function PredictionProbChart({
  data,
  xDomain,
  hoverTime = null,
  onHoverTimeChange,
  title = 'P(Up move)',
}: Props) {
  const plot = useMemo(() => {
    const [lo, hi] = xDomain
    const rows = data
      .filter((p) => Number.isFinite(p.t) && p.t >= lo - 5_000 && p.t <= hi + 5_000)
      .map((p) => ({
        t: p.t,
        p_up_3s: p.p_up_3s != null && Number.isFinite(p.p_up_3s) ? p.p_up_3s : null,
        p_up_5s: p.p_up_5s != null && Number.isFinite(p.p_up_5s) ? p.p_up_5s : null,
      }))
    // Keep at least the newest samples visible even if domain is still catching up.
    if (!rows.length && data.length) {
      return data.slice(-12).map((p) => ({
        t: p.t,
        p_up_3s: p.p_up_3s != null && Number.isFinite(p.p_up_3s) ? p.p_up_3s : null,
        p_up_5s: p.p_up_5s != null && Number.isFinite(p.p_up_5s) ? p.p_up_5s : null,
      }))
    }
    return rows
  }, [data, xDomain])

  const showDots = plot.length < 8

  return (
    <div className="prediction-prob-chart">
      <div className="chart-title-row">
        <div className="chart-title">{title}</div>
        <div className="prediction-prob-legend">
          <span className="prediction-prob-legend-3s">3s</span>
          <span className="prediction-prob-legend-5s">5s</span>
          <span className="muted">50% = flat</span>
        </div>
      </div>
      <div className="prediction-prob-plot">
        {plot.length === 0 ? (
          <div className="prediction-prob-empty muted">Waiting for prediction samples…</div>
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <LineChart
              data={plot}
              margin={CHART_MARGIN}
              onMouseMove={(state) => {
                const t = Number((state as { activeLabel?: number | string } | null)?.activeLabel)
                if (Number.isFinite(t)) onHoverTimeChange?.(t)
              }}
              onMouseLeave={() => onHoverTimeChange?.(null)}
            >
              <CartesianGrid strokeDasharray="3 3" stroke="#e4e6ed" vertical={false} />
              <XAxis
                dataKey="t"
                type="number"
                domain={xDomain}
                allowDataOverflow
                tickFormatter={formatTimeTick}
                tick={{ fill: '#6b6f7b', fontSize: 11 }}
                minTickGap={40}
              />
              <YAxis
                width={Y_AXIS_WIDTH}
                domain={[0, 1]}
                ticks={[0, 0.25, 0.5, 0.75, 1]}
                tickFormatter={formatPctTick}
                tick={{ fill: '#6b6f7b', fontSize: 11 }}
              />
              <Tooltip
                labelFormatter={(label) => formatTimeTick(Number(label))}
                formatter={(value, name) => {
                  const n = typeof value === 'number' ? value : Number(value)
                  const label = name === 'p_up_3s' ? '3s P(Up)' : '5s P(Up)'
                  return [Number.isFinite(n) ? `${(n * 100).toFixed(1)}%` : '—', label]
                }}
                contentStyle={{
                  borderRadius: 10,
                  border: '1px solid #e4e6ed',
                  boxShadow: '0 4px 16px rgba(15,17,23,0.08)',
                }}
              />
              <ReferenceLine y={0.5} stroke="#94a3b8" strokeDasharray="4 4" />
              {hoverTime != null && Number.isFinite(hoverTime) && (
                <ReferenceLine x={hoverTime} stroke="#94a3b8" strokeDasharray="3 3" />
              )}
              <Line
                type="monotone"
                dataKey="p_up_3s"
                name="p_up_3s"
                stroke="#10b981"
                strokeWidth={2.2}
                dot={showDots}
                activeDot={{ r: 4 }}
                connectNulls
                isAnimationActive={false}
              />
              <Line
                type="monotone"
                dataKey="p_up_5s"
                name="p_up_5s"
                stroke="#3b82f6"
                strokeWidth={2}
                strokeDasharray="5 4"
                dot={showDots}
                activeDot={{ r: 4 }}
                connectNulls
                isAnimationActive={false}
              />
            </LineChart>
          </ResponsiveContainer>
        )}
      </div>
    </div>
  )
}
