import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { formatCents } from '../api'

type Point = { t: number; btc?: number | null; twap?: number | null; up?: number | null; down?: number | null }

type Props = {
  data: Point[]
  priceToBeat?: number | null
  /** btc = Bitcoin price; outcomes = Up/Down probabilities */
  mode?: 'btc' | 'outcomes'
  /** Which BTC series to plot when mode=btc */
  btcKey?: 'btc' | 'twap'
  title?: string
}

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

type TipPayload = {
  dataKey?: string | number
  name?: string
  value?: number | string | null
  color?: string
  payload?: Point & { label?: string; upPct?: number | null; downPct?: number | null }
}

function outcomeLabel(dataKey: string | number | undefined, name: string | undefined): {
  label: string
  color: string
} {
  const key = String(dataKey ?? '')
  const n = String(name ?? '')
  if (key === 'upPct' || key === 'up' || n === 'Up') return { label: 'Up', color: '#10b981' }
  if (key === 'downPct' || key === 'down' || n === 'Down') return { label: 'Down', color: '#ef4444' }
  if (key === 'btc' || key === 'btcPlot' || n === 'BTC' || n === 'TWAP') {
    return { label: n === 'TWAP' || key === 'twap' ? 'TWAP' : 'BTC', color: '#f7931a' }
  }
  return { label: n || key || '—', color: '#6b7280' }
}

function ChartTooltip({
  active,
  payload,
  label,
  mode,
}: {
  active?: boolean
  payload?: TipPayload[]
  label?: string | number
  mode: 'btc' | 'outcomes'
}) {
  if (!active || !payload?.length) return null
  const items = payload
  const time =
    label != null
      ? String(label)
      : items[0]?.payload?.label ??
        (items[0]?.payload?.t != null
          ? new Date(items[0].payload!.t!).toLocaleTimeString(undefined, {
              hour: 'numeric',
              minute: '2-digit',
              second: '2-digit',
            })
          : '')

  return (
    <div className="chart-tooltip">
      <div className="chart-tooltip-time">{time}</div>
      {items.map((item, i) => {
        const { label: rowLabel, color } = outcomeLabel(item.dataKey, item.name)
        const raw = item.value
        const num = raw == null || raw === '' ? null : Number(raw)
        let valueText = '—'
        if (num != null && Number.isFinite(num)) {
          valueText =
            mode === 'btc'
              ? `$${num.toLocaleString(undefined, {
                  minimumFractionDigits: 2,
                  maximumFractionDigits: 2,
                })}`
              : formatCents(num / 100)
        }
        return (
          <div key={`${rowLabel}-${i}`} className="chart-tooltip-row" style={{ color }}>
            <span className="chart-tooltip-dot" style={{ background: color }} />
            <span className="chart-tooltip-name">{rowLabel}</span>
            <span className="chart-tooltip-value">{valueText}</span>
          </div>
        )
      })}
    </div>
  )
}

export default function PriceChart({
  data,
  priceToBeat,
  mode = 'btc',
  btcKey = 'btc',
  title,
}: Props) {
  const chartData = data.map((d) => ({
    ...d,
    btcPlot: btcKey === 'twap' ? d.twap : d.btc,
    upPct: d.up != null ? d.up * 100 : null,
    downPct: d.down != null ? d.down * 100 : null,
    label: new Date(d.t).toLocaleTimeString(undefined, {
      hour: 'numeric',
      minute: '2-digit',
      second: '2-digit',
    }),
  }))

  const btcValues = chartData.map((d) => d.btcPlot).filter((v): v is number => v != null)
  const btcMin = btcValues.length ? Math.min(...btcValues) : 0
  const btcMax = btcValues.length ? Math.max(...btcValues) : 1
  const pad = Math.max((btcMax - btcMin) * 0.15, 5)
  const showBtc = mode === 'btc'

  const lastBtc = btcValues.length ? btcValues[btcValues.length - 1] : null
  const aboveTarget =
    lastBtc != null && priceToBeat != null ? lastBtc >= priceToBeat : null

  const yMin =
    priceToBeat != null ? Math.min(btcMin - pad, priceToBeat - pad * 0.35) : btcMin - pad
  const yMax =
    priceToBeat != null ? Math.max(btcMax + pad, priceToBeat + pad * 0.35) : btcMax + pad

  return (
    <div className="chart-block">
      {title && <div className="chart-title">{title}</div>}
      <div className="chart-wrap">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={chartData} margin={{ top: 10, right: 8, left: 4, bottom: 0 }}>
            <CartesianGrid stroke="#eef0f4" strokeDasharray="0" vertical={false} />
            <XAxis
              dataKey="label"
              stroke="#9ca3af"
              tick={{ fontSize: 11, fill: '#9ca3af' }}
              minTickGap={48}
              axisLine={false}
              tickLine={false}
            />
            {showBtc ? (
              <>
                <YAxis
                  orientation="right"
                  domain={[yMin, yMax]}
                  stroke="#9ca3af"
                  tick={{ fontSize: 11, fill: '#9ca3af' }}
                  width={68}
                  axisLine={false}
                  tickLine={false}
                  tickFormatter={(v) =>
                    `$${Number(v).toLocaleString(undefined, { maximumFractionDigits: 0 })}`
                  }
                />
                <Tooltip
                  cursor={{ stroke: '#d1d5db', strokeWidth: 1 }}
                  content={<ChartTooltip mode="btc" />}
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
                <Line
                  type="monotone"
                  dataKey="btcPlot"
                  name={btcKey === 'twap' ? 'TWAP' : 'BTC'}
                  stroke="#f7931a"
                  dot={false}
                  activeDot={{ r: 4, fill: '#f7931a', stroke: '#fff', strokeWidth: 2 }}
                  strokeWidth={2.25}
                  isAnimationActive={false}
                  connectNulls
                />
              </>
            ) : (
              <>
                <YAxis
                  domain={[0, 100]}
                  stroke="#9ca3af"
                  tick={{ fontSize: 11, fill: '#9ca3af' }}
                  width={40}
                  axisLine={false}
                  tickLine={false}
                  tickFormatter={(v) => `${Number(v).toFixed(2)}¢`}
                />
                <Tooltip
                  cursor={{ stroke: '#d1d5db', strokeWidth: 1 }}
                  content={<ChartTooltip mode="outcomes" />}
                />
                <Legend />
                <Line
                  type="monotone"
                  dataKey="upPct"
                  name="Up"
                  stroke="#10b981"
                  dot={false}
                  activeDot={{ r: 4, fill: '#10b981', stroke: '#fff', strokeWidth: 2 }}
                  strokeWidth={2}
                  isAnimationActive={false}
                />
                <Line
                  type="monotone"
                  dataKey="downPct"
                  name="Down"
                  stroke="#ef4444"
                  dot={false}
                  activeDot={{ r: 4, fill: '#ef4444', stroke: '#fff', strokeWidth: 2 }}
                  strokeWidth={2}
                  isAnimationActive={false}
                />
              </>
            )}
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  )
}
