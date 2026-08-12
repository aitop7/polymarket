import { useEffect, useMemo, useState } from 'react'
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import {
  api,
  formatCents,
  formatUsd,
  formatWindowEt,
  type BacktestFill,
  type BacktestMarketRow,
  type BacktestResult,
  type MarketDetail,
} from '../api'
import ChartCollapseButton from '../components/ChartCollapseButton'
import PriceChart, {
  type BtcSeriesVisibility,
  type TimeDomain,
  type TraderMark,
} from '../components/PriceChart'
import VolumeChart from '../components/VolumeChart'

const DEFAULT_X_SPAN_MS = 180_000

type StrategyInfo = {
  name: string
  description: string
  params: Record<string, unknown>
}

function fmtAgo(ts: number, endMs: number): string {
  const sec = Math.max(0, Math.round((endMs - ts) / 1000))
  if (sec < 60) return `${sec}s`
  const m = Math.floor(sec / 60)
  const s = sec % 60
  return s ? `${m}m ${s}s` : `${m}m`
}

export default function BacktestPage() {
  const [strategies, setStrategies] = useState<StrategyInfo[]>([])
  const [collection, setCollection] = useState<'before_twap' | 'twap'>('twap')
  const [split, setSplit] = useState('validation')
  const [selectedDate, setSelectedDate] = useState('')
  const [dateMin, setDateMin] = useState('')
  const [dateMax, setDateMax] = useState('')
  const effectiveSplit = collection === 'twap' ? 'twap' : split
  const isTwap = collection === 'twap'
  const [strategy, setStrategy] = useState('lgbm_edge')
  const [limit, setLimit] = useState(20)
  const [startingCash, setStartingCash] = useState(1000)
  const [threshold, setThreshold] = useState(0.05)
  const [minEdge, setMinEdge] = useState(0.005)
  const [minAskShares, setMinAskShares] = useState(1)
  const [sizeUsd, setSizeUsd] = useState(25)
  const [maxPairs, setMaxPairs] = useState(5)
  const [cooldownSec, setCooldownSec] = useState(10)
  const [minElapsedSec, setMinElapsedSec] = useState(5)
  const [minRemainingSec, setMinRemainingSec] = useState(10)
  const [oncePerMarket, setOncePerMarket] = useState(false)
  const [feeModel, setFeeModel] = useState<'none' | 'polymarket' | 'flat'>('polymarket')
  const [slippage, setSlippage] = useState(0)
  const isSafePair = strategy === 'safe_pair'

  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<BacktestResult | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)

  const [detail, setDetail] = useState<MarketDetail | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [btcVisible, setBtcVisible] = useState<BtcSeriesVisibility>({
    twap: true,
    chainlink: true,
    binance: true,
  })
  const [sharedXDomain, setSharedXDomain] = useState<TimeDomain | null>(null)
  const [followLiveX, setFollowLiveX] = useState(true)
  const [equityCollapsed, setEquityCollapsed] = useState(false)

  useEffect(() => {
    api
      .strategies()
      .then((list) => {
        const usable = list.filter((s) => s.name !== 'none')
        setStrategies(usable)
        if (usable.length && !usable.some((s) => s.name === strategy)) {
          setStrategy(usable[0].name)
        }
      })
      .catch(() => setStrategies([]))
  }, [])

  useEffect(() => {
    let cancelled = false
    api
      .marketDates(effectiveSplit)
      .then((res) => {
        if (cancelled) return
        setDateMin(res.min || '')
        setDateMax(res.max || '')
        if (isTwap) {
          const next = res.max || res.dates[res.dates.length - 1] || ''
          setSelectedDate((prev) =>
            prev && res.dates.includes(prev) ? prev : next,
          )
        }
      })
      .catch(() => {
        if (!cancelled) {
          setDateMin('')
          setDateMax('')
        }
      })
    return () => {
      cancelled = true
    }
  }, [effectiveSplit, isTwap])

  const run = async () => {
    setLoading(true)
    setError(null)
    setSelectedId(null)
    setDetail(null)
    try {
      const res = await api.backtest({
        strategy,
        split: effectiveSplit,
        limit,
        starting_cash: startingCash,
        ...(isTwap && selectedDate ? { date: selectedDate } : {}),
        params: isSafePair
          ? {
              min_edge: minEdge,
              size_usd: sizeUsd,
              min_ask_shares: minAskShares,
              max_pairs_per_market: oncePerMarket ? 1 : maxPairs,
              cooldown_seconds: cooldownSec,
              min_elapsed_seconds: minElapsedSec,
              min_remaining_seconds: minRemainingSec,
              once_per_market: oncePerMarket,
              taker_fee_rate: 0.07,
              fee_model: feeModel,
              slippage,
            }
          : {
              threshold,
              size_usd: sizeUsd,
              once_per_market: oncePerMarket,
              max_trades_per_market: oncePerMarket ? 1 : maxPairs,
              cooldown_seconds: cooldownSec,
              min_elapsed_seconds: minElapsedSec,
              min_remaining_seconds: minRemainingSec,
            },
      })
      setResult(res)
      if (res.markets?.length) {
        setSelectedId(res.markets[0].market_id)
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      setResult(null)
    } finally {
      setLoading(false)
    }
  }

  const selected: BacktestMarketRow | null = useMemo(() => {
    if (!result || !selectedId) return null
    return result.markets.find((m) => m.market_id === selectedId) ?? null
  }, [result, selectedId])

  useEffect(() => {
    if (!selectedId || !result) {
      setDetail(null)
      return
    }
    let cancelled = false
    setDetailLoading(true)
    setFollowLiveX(true)
    setSharedXDomain(null)
    api
      .market(selectedId, result.split)
      .then((d) => {
        if (!cancelled) setDetail(d)
      })
      .catch(() => {
        if (!cancelled) setDetail(null)
      })
      .finally(() => {
        if (!cancelled) setDetailLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [selectedId, result?.split])

  const chartData = useMemo(() => {
    return (detail?.series ?? []).map((p) => ({
      t: p.t,
      up: p.up ?? null,
      down: p.down ?? null,
      btc: p.btc,
      twap: p.twap ?? null,
      chainlink: p.chainlink ?? null,
      bn_buy: p.bn_buy ?? 0,
      bn_sell: p.bn_sell ?? 0,
      up_buy_vol: p.up_buy_vol ?? 0,
      up_sell_vol: p.up_sell_vol ?? 0,
      down_buy_vol: p.down_buy_vol ?? 0,
      down_sell_vol: p.down_sell_vol ?? 0,
    }))
  }, [detail])

  const xFullDomain = useMemo((): TimeDomain => {
    if (detail?.start_time != null && detail?.end_time != null) {
      return [detail.start_time, detail.end_time]
    }
    if (chartData.length >= 2) {
      return [chartData[0].t, chartData[chartData.length - 1].t]
    }
    return [0, DEFAULT_X_SPAN_MS]
  }, [detail, chartData])

  const xDefaultDomain = useMemo((): TimeDomain => {
    const [f0, f1] = xFullDomain
    return [f0, f1 > f0 ? f1 : f0 + 300_000]
  }, [xFullDomain])

  useEffect(() => {
    setFollowLiveX(true)
    setSharedXDomain(null)
  }, [selectedId])

  const activeXDomain = followLiveX ? xDefaultDomain : (sharedXDomain ?? xDefaultDomain)

  const traderMarks = useMemo((): TraderMark[] => {
    const fills = selected?.fills ?? []
    return fills
      .map((f) => {
        const sideRaw = String(f.side || '').toUpperCase()
        const actionRaw = String(f.action || 'BUY').toUpperCase()
        const outcome: 'Up' | 'Down' = sideRaw === 'DOWN' ? 'Down' : 'Up'
        const side: 'BUY' | 'SELL' = actionRaw === 'SELL' ? 'SELL' : 'BUY'
        return {
          t: Number(f.timestamp),
          pricePct: Number(f.price) * 100,
          side,
          outcome,
        }
      })
      .filter((m) => Number.isFinite(m.t) && Number.isFinite(m.pricePct))
  }, [selected])

  const windowLabel =
    detail?.start_time != null && detail?.end_time != null
      ? formatWindowEt(detail.start_time, detail.end_time)
      : selected
        ? `Market ${selected.market_id}`
        : 'Select a market after running'

  const fills: BacktestFill[] = (selected?.signals?.length ? selected.signals : selected?.fills) ?? []
  const fillEnd = detail?.end_time ?? fills[fills.length - 1]?.timestamp ?? Date.now()

  const equity = result?.equity_curve ?? []
  const pnlPositive = (result?.total_pnl ?? 0) >= 0

  return (
    <div className="workspace">
      <aside className="workspace-rail workspace-rail-left">
        <div className="control-sidebar control-sidebar-embedded">
          <div className="sidebar-section">
            <div className="sidebar-heading mode-heading">
              <span>Backtest</span>
              <span className="mode-current-pill">Batch</span>
            </div>
            <p className="muted" style={{ margin: '0 0 0.55rem', fontSize: '0.78rem', lineHeight: 1.4 }}>
              Replay strategies over historical markets (fetch_real splits or TWAP fetch_live data).
            </p>

            <div className="sidebar-heading data-heading" style={{ marginTop: '0.35rem' }}>
              <span>Data</span>
            </div>
            <div className="data-segment" role="group" aria-label="Collection">
              <button
                type="button"
                className={`data-segment-btn${collection === 'twap' ? ' active' : ''}`}
                disabled={loading}
                onClick={() => setCollection('twap')}
                aria-pressed={collection === 'twap'}
              >
                TWAP
              </button>
              <button
                type="button"
                className={`data-segment-btn${collection === 'before_twap' ? ' active' : ''}`}
                disabled={loading}
                onClick={() => setCollection('before_twap')}
                aria-pressed={collection === 'before_twap'}
              >
                Before TWAP
              </button>
            </div>

            {isTwap ? (
              <>
                <label className="sidebar-label">Date (ET)</label>
                <input
                  type="date"
                  value={selectedDate}
                  min={dateMin || undefined}
                  max={dateMax || undefined}
                  disabled={loading || !dateMin}
                  onChange={(e) => setSelectedDate(e.target.value)}
                />
                {dateMin && dateMax && (
                  <p className="muted" style={{ margin: '0.35rem 0 0', fontSize: '0.72rem' }}>
                    {dateMin.slice(5)} → {dateMax.slice(5)}
                  </p>
                )}
              </>
            ) : (
              <>
                <label className="sidebar-label">Split</label>
                <select value={split} onChange={(e) => setSplit(e.target.value)} disabled={loading}>
                  <option value="validation">validation</option>
                  <option value="test">test</option>
                  <option value="train">train</option>
                </select>
              </>
            )}

            <label className="sidebar-label">Strategy</label>
            <select
              value={strategy}
              onChange={(e) => setStrategy(e.target.value)}
              disabled={loading}
            >
              {(strategies.length
                ? strategies
                : [
                    { name: 'lgbm_edge', description: '', params: {} },
                    { name: 'edge_threshold', description: '', params: {} },
                  ]
              ).map((s) => (
                <option key={s.name} value={s.name}>
                  {s.name}
                </option>
              ))}
            </select>

            <label className="sidebar-label">Markets</label>
            <input
              type="number"
              min={1}
              max={200}
              value={limit}
              disabled={loading}
              onChange={(e) => setLimit(Math.max(1, Math.min(200, Number(e.target.value) || 1)))}
            />

            <label className="sidebar-label">Starting cash (shared)</label>
            <input
              type="number"
              min={1}
              step={10}
              value={startingCash}
              disabled={loading}
              onChange={(e) => setStartingCash(Math.max(1, Number(e.target.value) || 1))}
            />
            <p className="muted" style={{ margin: '0 0 0.5rem', fontSize: '0.72rem', lineHeight: 1.35 }}>
              One wallet across all markets in the batch.
            </p>
          </div>

          <div className="sidebar-section">
            <div className="sidebar-heading">Params</div>
            <label className="sidebar-label">Skip first (sec)</label>
            <input
              type="number"
              step={1}
              min={0}
              max={120}
              value={minElapsedSec}
              disabled={loading}
              onChange={(e) => setMinElapsedSec(Math.max(0, Number(e.target.value) || 0))}
            />
            <label className="sidebar-label">Skip last (sec)</label>
            <input
              type="number"
              step={1}
              min={0}
              max={120}
              value={minRemainingSec}
              disabled={loading}
              onChange={(e) => setMinRemainingSec(Math.max(0, Number(e.target.value) || 0))}
            />
            <p className="muted" style={{ margin: '0 0 0.55rem', fontSize: '0.72rem', lineHeight: 1.35 }}>
              No trades while Up/Down quotes are unreliable at market open or close.
            </p>
            {isSafePair ? (
              <>
                <label className="sidebar-label">Min net edge</label>
                <input
                  type="number"
                  step={0.001}
                  min={0}
                  max={0.5}
                  value={minEdge}
                  disabled={loading}
                  onChange={(e) => setMinEdge(Number(e.target.value))}
                />
                <label className="sidebar-label">Min ask shares</label>
                <input
                  type="number"
                  step={1}
                  min={1}
                  value={minAskShares}
                  disabled={loading}
                  onChange={(e) => setMinAskShares(Math.max(1, Number(e.target.value) || 1))}
                />
                <label className="sidebar-label">Max pairs / market</label>
                <input
                  type="number"
                  step={1}
                  min={1}
                  max={50}
                  value={oncePerMarket ? 1 : maxPairs}
                  disabled={loading || oncePerMarket}
                  onChange={(e) => setMaxPairs(Math.max(1, Math.min(50, Number(e.target.value) || 1)))}
                />
                <label className="sidebar-label">Cooldown (sec)</label>
                <input
                  type="number"
                  step={1}
                  min={0}
                  max={120}
                  value={cooldownSec}
                  disabled={loading}
                  onChange={(e) => setCooldownSec(Math.max(0, Number(e.target.value) || 0))}
                />
                <label className="sidebar-label">Fee model</label>
                <select
                  value={feeModel}
                  disabled={loading}
                  onChange={(e) => setFeeModel(e.target.value as 'none' | 'polymarket' | 'flat')}
                >
                  <option value="polymarket">Polymarket crypto (0.07)</option>
                  <option value="flat">Flat % of notional</option>
                  <option value="none">None</option>
                </select>
                <p className="muted" style={{ margin: '0 0 0.5rem', fontSize: '0.72rem', lineHeight: 1.35 }}>
                  C×0.07×p×(1−p) per leg; peaks at 50¢ (~$1.75 / 100 shares).
                </p>
                <label className="sidebar-label">Slippage ($/share)</label>
                <input
                  type="number"
                  step={0.001}
                  min={0}
                  max={0.05}
                  value={slippage}
                  disabled={loading}
                  onChange={(e) => setSlippage(Math.max(0, Number(e.target.value) || 0))}
                />
                <label className="sidebar-check">
                  <input
                    type="checkbox"
                    checked={oncePerMarket}
                    disabled={loading}
                    onChange={(e) => setOncePerMarket(e.target.checked)}
                  />
                  Once per market
                </label>
              </>
            ) : (
              <>
                <label className="sidebar-label">Edge threshold</label>
                <input
                  type="number"
                  step={0.01}
                  min={0}
                  max={0.5}
                  value={threshold}
                  disabled={loading}
                  onChange={(e) => setThreshold(Number(e.target.value))}
                />
                <label className="sidebar-label">Max trades / market</label>
                <input
                  type="number"
                  step={1}
                  min={1}
                  max={20}
                  value={oncePerMarket ? 1 : maxPairs}
                  disabled={loading || oncePerMarket}
                  onChange={(e) => setMaxPairs(Math.max(1, Math.min(20, Number(e.target.value) || 1)))}
                />
                <label className="sidebar-label">Cooldown (sec)</label>
                <input
                  type="number"
                  step={1}
                  min={0}
                  max={120}
                  value={cooldownSec}
                  disabled={loading}
                  onChange={(e) => setCooldownSec(Math.max(0, Number(e.target.value) || 0))}
                />
              </>
            )}
            <label className="sidebar-label">Size (USD)</label>
            <input
              type="number"
              step={1}
              min={1}
              value={sizeUsd}
              disabled={loading}
              onChange={(e) => setSizeUsd(Math.max(1, Number(e.target.value) || 1))}
            />
            {!isSafePair && (
              <>
                <p className="muted" style={{ margin: '0 0 0.5rem', fontSize: '0.72rem', lineHeight: 1.35 }}>
                  Max notional per order (shares = USD ÷ ask). Polymarket crypto fees apply.
                </p>
                <label className="sidebar-check">
                  <input
                    type="checkbox"
                    checked={oncePerMarket}
                    disabled={loading}
                    onChange={(e) => setOncePerMarket(e.target.checked)}
                  />
                  Once per market
                </label>
              </>
            )}
          </div>

          <div className="sidebar-section sidebar-section-last">
            <button
              type="button"
              className="sidebar-btn primary"
              style={{ width: '100%' }}
              onClick={() => void run()}
              disabled={loading}
            >
              {loading ? 'Running…' : 'Run backtest'}
            </button>
          </div>
        </div>
      </aside>

      <div className="workspace-main">
        {error && <p className="error">{error}</p>}

        <section className="btc-panel">
          <div className="btc-panel-sticky">
            <div className="btc-panel-identity">
              <div className="btc-logo" aria-hidden>
                Σ
              </div>
              <div className="btc-panel-identity-text">
                <h1 className="btc-panel-title">
                  Backtest
                  {result ? (
                    <span className="btc-market-id">
                      ({result.strategy} · {result.split}
                      {result.date ? ` · ${result.date}` : ''})
                    </span>
                  ) : null}
                </h1>
                <div className="btc-panel-sub">
                  {result
                    ? `${result.n_markets} markets · ${result.total_fills} fills`
                    : 'Configure and run a strategy over a split'}
                </div>
              </div>
            </div>
            {result && (
              <div className={`btc-panel-outcome ${pnlPositive ? 'up' : 'down'}`}>
                <div className="btc-outcome-label">Total PnL</div>
                <div className="btc-outcome-value">
                  {pnlPositive ? '+' : ''}
                  {formatUsd(result.total_pnl)}
                </div>
              </div>
            )}
          </div>

          <div className="price-beat-row" style={{ marginTop: '0.75rem' }}>
            <div className="stat-card">
              <div className="label">Win rate</div>
              <div className="value">
                {result ? `${(result.win_rate * 100).toFixed(1)}%` : '—'}
              </div>
            </div>
            <div className="stat-card">
              <div className="label">Avg PnL</div>
              <div className={`value ${!result ? '' : result.avg_pnl >= 0 ? 'up' : 'down'}`}>
                {result ? formatUsd(result.avg_pnl) : '—'}
              </div>
            </div>
            <div className="stat-card">
              <div className="label">Ending cash</div>
              <div className="value">
                {result
                  ? result.shared_bankroll
                    ? formatUsd(result.ending_cash ?? result.starting_cash + result.total_pnl)
                    : '—'
                  : '—'}
              </div>
            </div>
            <div className="stat-card">
              <div className="label">Selected</div>
              <div className="value" style={{ fontSize: '0.95rem' }}>
                {selected ? selected.market_id : '—'}
              </div>
            </div>
          </div>

          {result?.stats && result.strategy === 'safe_pair' && (
            <div
              className="price-beat-row backtest-stats-row"
              style={{ marginTop: '0.5rem', gridTemplateColumns: 'repeat(5, 1fr)' }}
            >
              <div className="stat-card">
                <div className="label">Opportunities</div>
                <div className="value">{result.stats.opportunities_found}</div>
              </div>
              <div className="stat-card">
                <div className="label">Markets w/ opp</div>
                <div className="value">{result.stats.markets_with_opportunities}</div>
              </div>
              <div className="stat-card">
                <div className="label">Pairs filled</div>
                <div className="value">{result.stats.pairs_filled}</div>
              </div>
              <div className="stat-card">
                <div className="label">Avg net edge</div>
                <div className="value">
                  {result.stats.avg_net_edge != null
                    ? `${(result.stats.avg_net_edge * 100).toFixed(2)}¢`
                    : '—'}
                </div>
              </div>
              <div className="stat-card">
                <div className="label">Fill rate</div>
                <div className="value">
                  {result.stats.fill_rate != null
                    ? `${(result.stats.fill_rate * 100).toFixed(1)}%`
                    : '—'}
                </div>
              </div>
            </div>
          )}
        </section>

        <div className={`chart-block${equityCollapsed ? ' chart-block-collapsed' : ''}`}>
          <div className="chart-header">
            <div className="chart-header-left">
              <div className="chart-title-row">
                <ChartCollapseButton
                  collapsed={equityCollapsed}
                  onToggle={() => setEquityCollapsed((v) => !v)}
                  label="Equity curve"
                />
                <div className="chart-title">Equity curve</div>
              </div>
            </div>
          </div>
          {!equityCollapsed && (
            <div className="chart-wrap" style={{ height: 220 }}>
              {equity.length ? (
                <ResponsiveContainer width="100%" height="100%">
                  <ComposedChart data={equity} margin={{ top: 10, right: 8, left: 4, bottom: 8 }}>
                    <CartesianGrid stroke="#eef0f4" vertical={false} />
                    <XAxis dataKey="i" tick={{ fontSize: 11, fill: '#9ca3af' }} axisLine={false} tickLine={false} />
                    <YAxis
                      stroke="#9ca3af"
                      tick={{ fontSize: 11, fill: '#9ca3af' }}
                      width={56}
                      axisLine={false}
                      tickLine={false}
                      tickFormatter={(v) => formatUsd(Number(v), 0)}
                    />
                    <Tooltip
                      contentStyle={{
                        background: '#ffffff',
                        border: '1px solid #e4e6ed',
                        borderRadius: 8,
                        boxShadow: '0 4px 12px rgba(15,17,23,0.08)',
                      }}
                      formatter={(value) => [formatUsd(Number(value ?? 0)), 'Cum PnL']}
                      labelFormatter={(_, payload) => {
                        const row = payload?.[0]?.payload as { market_id?: string; i?: number } | undefined
                        return row?.market_id ? `#${row.i} · ${row.market_id}` : ''
                      }}
                    />
                    <Area
                      type="monotone"
                      dataKey="cum_pnl"
                      stroke="#3b82f6"
                      fill="#3b82f622"
                      strokeWidth={2}
                      dot={false}
                    />
                    <Line type="monotone" dataKey="cum_pnl" stroke="#3b82f6" dot={false} strokeWidth={0} />
                  </ComposedChart>
                </ResponsiveContainer>
              ) : (
                <div className="chart-empty muted">Run a backtest to see cumulative PnL.</div>
              )}
            </div>
          )}
        </div>

        {selected && (
          <>
            <div className="btc-panel-sub" style={{ margin: '0.85rem 0 0.35rem' }}>
              {windowLabel}
              {detailLoading ? ' · loading series…' : ''}
            </div>
            {chartData.length > 0 ? (
              <>
                <PriceChart
                  data={chartData}
                  mode="btc"
                  title="BTC PRICE"
                  priceToBeat={detail?.btc_open_price}
                  xDomain={activeXDomain}
                  onXDomainChange={(next) => {
                    setFollowLiveX(false)
                    setSharedXDomain(next)
                  }}
                  onXDomainReset={() => {
                    setFollowLiveX(true)
                    setSharedXDomain(null)
                  }}
                  xFullDomain={xFullDomain}
                  xDefaultDomain={xDefaultDomain}
                  seriesVisible={btcVisible}
                  onSeriesVisibleChange={setBtcVisible}
                />
                <VolumeChart data={chartData} mode="binance" title="BINANCE BTC VOLUME" xDomain={activeXDomain} />
                <PriceChart
                  data={chartData}
                  mode="outcomes"
                  title="UP / DOWN PRICE"
                  xDomain={activeXDomain}
                  onXDomainChange={(next) => {
                    setFollowLiveX(false)
                    setSharedXDomain(next)
                  }}
                  onXDomainReset={() => {
                    setFollowLiveX(true)
                    setSharedXDomain(null)
                  }}
                  xFullDomain={xFullDomain}
                  xDefaultDomain={xDefaultDomain}
                  traderMarks={traderMarks}
                />
                <VolumeChart data={chartData} mode="outcomes" title="UP / DOWN VOLUME" xDomain={activeXDomain} />
              </>
            ) : (
              !detailLoading && (
                <p className="muted" style={{ marginTop: '0.75rem' }}>
                  No chart series for this market (features/training parquet missing).
                </p>
              )
            )}
          </>
        )}

        <div className="panel backtest-markets-panel" style={{ marginTop: '1rem' }}>
          <div className="sidebar-heading" style={{ marginBottom: '0.65rem' }}>
            Markets
          </div>
          {!result?.markets?.length ? (
            <p className="muted" style={{ margin: 0 }}>
              No results yet.
            </p>
          ) : (
            <div className="backtest-markets-scroll">
              <table className="table">
                <thead>
                  <tr>
                    <th>#</th>
                    <th>Market</th>
                    <th>Winner</th>
                    <th>Fills</th>
                    <th>PnL</th>
                  </tr>
                </thead>
                <tbody>
                  {result.markets.map((m, i) => (
                    <tr
                      key={m.market_id}
                      className={m.market_id === selectedId ? 'row-selected' : undefined}
                      onClick={() => setSelectedId(m.market_id)}
                      style={{ cursor: 'pointer' }}
                    >
                      <td>{i + 1}</td>
                      <td>{m.market_id}</td>
                      <td>{m.winner === 1 ? 'UP' : 'DOWN'}</td>
                      <td>{m.n_fills}</td>
                      <td className={m.pnl >= 0 ? 'success' : 'error'}>{formatUsd(m.pnl)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>

      <aside className="workspace-rail workspace-rail-right">
        <section className="activity-panel">
          <div className="activity-panel-header">
            <div className="activity-panel-title">Fills</div>
            {selected && (
              <div className={`time-window-badge ${selected.pnl >= 0 ? 'health-great' : 'health-bad'}`}>
                {formatUsd(selected.pnl)}
              </div>
            )}
          </div>
          {!selected ? (
            <p className="activity-panel-empty">Select a market from the results table</p>
          ) : fills.length === 0 ? (
            <p className="activity-panel-empty">No fills in this market</p>
          ) : (
            <ul className="activity-tape">
              {[...fills].reverse().map((f, i) => {
                const side = String(f.side || '').toUpperCase()
                const action = String(f.action || 'BUY').toUpperCase()
                const isDown = side === 'DOWN'
                const isSell = action === 'SELL'
                return (
                  <li key={`${f.timestamp}-${i}`} className="activity-tape-row">
                    <div className="activity-tape-body">
                      <div className="activity-tape-line">
                        <strong className="activity-tape-name">{isSell ? 'Sell' : 'Buy'}</strong>{' '}
                        <span className="activity-tape-action">
                          {isDown ? 'Down' : 'Up'} @ {formatCents(Number(f.price))}
                        </span>
                      </div>
                      <div className="activity-tape-meta">
                        {Number(f.shares || 0).toFixed(2)} sh
                        {f.reason ? ` · ${f.reason}` : ''}
                        {f.model_p_up != null ? ` · p=${Number(f.model_p_up).toFixed(3)}` : ''}
                      </div>
                    </div>
                    <div className="activity-tape-right">
                      <span className="activity-tape-ago">
                        {f.timestamp != null ? fmtAgo(Number(f.timestamp), Number(fillEnd)) : '—'}
                      </span>
                    </div>
                  </li>
                )
              })}
            </ul>
          )}
        </section>

        {selected && (
          <section className="control-sidebar control-sidebar-embedded">
            <div className="sidebar-heading">Market summary</div>
            <div className="price-beat-row" style={{ gridTemplateColumns: '1fr 1fr' }}>
              <div className="stat-card">
                <div className="label">Winner</div>
                <div className={`value ${selected.winner === 1 ? 'up' : 'down'}`}>
                  {selected.winner === 1 ? 'UP' : 'DOWN'}
                </div>
              </div>
              <div className="stat-card">
                <div className="label">Ending cash</div>
                <div className="value">{formatUsd(selected.ending_cash)}</div>
              </div>
            </div>
          </section>
        )}
      </aside>
    </div>
  )
}
