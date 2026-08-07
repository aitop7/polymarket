import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

type Point = { t: number; btc?: number | null; up?: number | null; down?: number | null }

export default function PriceChart({ data }: { data: Point[] }) {
  const chartData = data.map((d) => ({
    ...d,
    upPct: d.up != null ? d.up * 100 : null,
    downPct: d.down != null ? d.down * 100 : null,
    label: new Date(d.t).toLocaleTimeString(undefined, { minute: '2-digit', second: '2-digit' }),
  }))

  return (
    <div className="chart-wrap">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={chartData} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
          <CartesianGrid stroke="#2a2b36" strokeDasharray="3 3" />
          <XAxis dataKey="label" stroke="#9a9baf" tick={{ fontSize: 11 }} minTickGap={40} />
          <YAxis
            yAxisId="prob"
            domain={[0, 100]}
            stroke="#9a9baf"
            tick={{ fontSize: 11 }}
            width={36}
            tickFormatter={(v) => `${v}%`}
          />
          <Tooltip
            contentStyle={{ background: '#1a1b23', border: '1px solid #2a2b36', borderRadius: 8 }}
            labelStyle={{ color: '#9a9baf' }}
          />
          <Legend />
          <Line
            yAxisId="prob"
            type="monotone"
            dataKey="upPct"
            name="Up %"
            stroke="#3dd68c"
            dot={false}
            strokeWidth={2}
            isAnimationActive={false}
          />
          <Line
            yAxisId="prob"
            type="monotone"
            dataKey="downPct"
            name="Down %"
            stroke="#ff6b8a"
            dot={false}
            strokeWidth={2}
            isAnimationActive={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}
