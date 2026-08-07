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

type Point = { t: number; btc?: number | null; up?: number | null; down?: number | null }

type Props = {
  data: Point[]
  priceToBeat?: number | null
  /** btc = Bitcoin price; outcomes = Up/Down probabilities */
  mode?: 'btc' | 'outcomes'
  title?: string
}

const tooltipStyle = {
  background: '#ffffff',
  border: '1px solid #e4e6ed',
  borderRadius: 8,
  boxShadow: '0 4px 12px rgba(15,17,23,0.08)',
}

export default function PriceChart({ data, priceToBeat, mode = 'btc', title }: Props) {
  const chartData = data.map((d) => ({
    ...d,
    upPct: d.up != null ? d.up * 100 : null,
    downPct: d.down != null ? d.down * 100 : null,
    label: new Date(d.t).toLocaleTimeString(undefined, {
      hour: 'numeric',
      minute: '2-digit',
      second: '2-digit',
    }),
  }))

  const btcValues = chartData.map((d) => d.btc).filter((v): v is number => v != null)
  const btcMin = btcValues.length ? Math.min(...btcValues) : 0
  const btcMax = btcValues.length ? Math.max(...btcValues) : 1
  const pad = Math.max((btcMax - btcMin) * 0.15, 5)
  const showBtc = mode === 'btc'

  return (
    <div className="chart-block">
      {title && <div className="chart-title">{title}</div>}
      <div className="chart-wrap">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={chartData} margin={{ top: 8, right: 12, left: 4, bottom: 0 }}>
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
                  domain={[btcMin - pad, btcMax + pad]}
                  stroke="#9ca3af"
                  tick={{ fontSize: 11, fill: '#9ca3af' }}
                  width={64}
                  axisLine={false}
                  tickLine={false}
                  tickFormatter={(v) =>
                    Number(v).toLocaleString(undefined, { maximumFractionDigits: 0 })
                  }
                />
                <Tooltip
                  contentStyle={tooltipStyle}
                  labelStyle={{ color: '#6b6f7b' }}
                  itemStyle={{ color: '#0f1117' }}
                />
                {priceToBeat != null && (
                  <ReferenceLine
                    y={priceToBeat}
                    stroke="#f7931a"
                    strokeDasharray="4 4"
                    strokeWidth={1.5}
                    label={{ value: 'Target', fill: '#f7931a', fontSize: 11, position: 'insideTopRight' }}
                  />
                )}
                <Line
                  type="monotone"
                  dataKey="btc"
                  name="BTC"
                  stroke="#f7931a"
                  dot={false}
                  strokeWidth={2.25}
                  isAnimationActive={false}
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
                  tickFormatter={(v) => `${v}¢`}
                />
                <Tooltip
                  contentStyle={tooltipStyle}
                  labelStyle={{ color: '#6b6f7b' }}
                  formatter={(value, name) => [
                    value != null ? `${Number(value).toFixed(1)}¢` : '—',
                    name === 'upPct' ? 'Up' : 'Down',
                  ]}
                />
                <Legend />
                <Line
                  type="monotone"
                  dataKey="upPct"
                  name="Up"
                  stroke="#10b981"
                  dot={false}
                  strokeWidth={2}
                  isAnimationActive={false}
                />
                <Line
                  type="monotone"
                  dataKey="downPct"
                  name="Down"
                  stroke="#ef4444"
                  dot={false}
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
