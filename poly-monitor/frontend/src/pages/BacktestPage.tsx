import { useState } from 'react'
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { api, formatUsd } from '../api'

export default function BacktestPage() {
  const [split, setSplit] = useState('validation')
  const [strategy, setStrategy] = useState('lgbm_edge')
  const [limit, setLimit] = useState(20)
  const [threshold, setThreshold] = useState(0.05)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<Record<string, unknown> | null>(null)

  const run = async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await api.backtest({
        strategy,
        split,
        limit,
        starting_cash: 1000,
        params: { threshold, size_usd: 10, once_per_market: true },
      })
      setResult(res)
    } catch (e) {
      setError(String(e))
    } finally {
      setLoading(false)
    }
  }

  const markets = (result?.markets as { market_id: string; pnl: number; n_fills: number; winner: number }[]) || []
  const equity = (result?.equity_curve as { market_id: string; cum_pnl: number }[]) || []

  return (
    <div>
      <h1 style={{ marginTop: 0 }}>Backtest</h1>
      <p className="muted">Run a strategy over historical markets from `fetch_real` features.</p>

      <div className="controls">
        <div>
          <label className="muted">Split</label>
          <select value={split} onChange={(e) => setSplit(e.target.value)}>
            <option value="validation">validation</option>
            <option value="test">test</option>
            <option value="train">train</option>
          </select>
        </div>
        <div>
          <label className="muted">Strategy</label>
          <select value={strategy} onChange={(e) => setStrategy(e.target.value)}>
            <option value="lgbm_edge">lgbm_edge</option>
            <option value="edge_threshold">edge_threshold</option>
          </select>
        </div>
        <div>
          <label className="muted">Markets</label>
          <input type="number" min={1} max={200} value={limit} onChange={(e) => setLimit(Number(e.target.value))} />
        </div>
        <div>
          <label className="muted">Threshold</label>
          <input
            type="number"
            step={0.01}
            min={0}
            max={0.5}
            value={threshold}
            onChange={(e) => setThreshold(Number(e.target.value))}
          />
        </div>
        <button type="button" onClick={run} disabled={loading}>
          {loading ? 'Running…' : 'Run backtest'}
        </button>
      </div>

      {error && <p className="error">{error}</p>}

      {result && (
        <>
          <div className="price-beat-row">
            <div className="stat-card">
              <div className="label">Total PnL</div>
              <div className={`value ${Number(result.total_pnl) >= 0 ? 'up' : 'down'}`}>
                {formatUsd(Number(result.total_pnl))}
              </div>
            </div>
            <div className="stat-card">
              <div className="label">Win rate / fills</div>
              <div className="value">
                {(Number(result.win_rate) * 100).toFixed(1)}% · {String(result.total_fills)}
              </div>
            </div>
          </div>

          <div className="panel" style={{ marginBottom: '1rem' }}>
            <div className="chart-wrap">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={equity}>
                  <CartesianGrid stroke="#2a2b36" strokeDasharray="3 3" />
                  <XAxis dataKey="market_id" hide />
                  <YAxis stroke="#9a9baf" tick={{ fontSize: 11 }} width={48} />
                  <Tooltip
                    contentStyle={{ background: '#1a1b23', border: '1px solid #2a2b36', borderRadius: 8 }}
                  />
                  <Line type="monotone" dataKey="cum_pnl" stroke="#5b8def" dot={false} strokeWidth={2} />
                </LineChart>
              </ResponsiveContainer>
            </div>
          </div>

          <div className="panel">
            <table className="table">
              <thead>
                <tr>
                  <th>Market</th>
                  <th>Winner</th>
                  <th>Fills</th>
                  <th>PnL</th>
                </tr>
              </thead>
              <tbody>
                {markets.map((m) => (
                  <tr key={m.market_id}>
                    <td>{m.market_id}</td>
                    <td>{m.winner === 1 ? 'UP' : 'DOWN'}</td>
                    <td>{m.n_fills}</td>
                    <td className={m.pnl >= 0 ? 'success' : 'error'}>{formatUsd(m.pnl)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  )
}
