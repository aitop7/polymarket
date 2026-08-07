import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  api,
  formatPct,
  formatUsd,
  formatWindow,
  type MarketDetail,
  type MarketSummary,
  wsUrl,
} from '../api'
import PriceChart from '../components/PriceChart'
import TradePanel from '../components/TradePanel'

type Tick = {
  type: string
  timestamp: number
  btc_price: number | null
  btc_open: number | null
  up_price: number
  down_price: number
  remaining_seconds: number
  elapsed_seconds: number
  model_p_up?: number | null
  portfolio?: { cash: number; up_shares: number; down_shares: number }
  equity?: number
  fills?: { side: string; action: string; price: number; shares: number; reason: string; source: string }[]
  settlement?: { winner: number; ending_cash: number }
}

type FillRow = {
  side: string
  action: string
  price: number
  shares: number
  reason?: string
  source?: string
  timestamp?: number
}

type Props = {
  mode: 'monitor' | 'paper'
}

export default function MarketPage({ mode }: Props) {
  const [split, setSplit] = useState('validation')
  const [markets, setMarkets] = useState<MarketSummary[]>([])
  const [marketId, setMarketId] = useState<string>('')
  const [detail, setDetail] = useState<MarketDetail | null>(null)
  const [neighbors, setNeighbors] = useState<{ prev: string | null; next: string | null }>({
    prev: null,
    next: null,
  })
  const [speed, setSpeed] = useState(30)
  const [strategy, setStrategy] = useState(mode === 'paper' ? 'lgbm_edge' : 'none')
  const [playing, setPlaying] = useState(false)
  const [tick, setTick] = useState<Tick | null>(null)
  const [seriesLive, setSeriesLive] = useState<{ t: number; up: number; down: number; btc: number | null }[]>(
    [],
  )
  const [activity, setActivity] = useState<FillRow[]>([])
  const [tab, setTab] = useState<'book' | 'activity' | 'positions' | 'rules'>('activity')
  const [book, setBook] = useState<Record<string, unknown> | null>(null)
  const [side, setSide] = useState<'UP' | 'DOWN'>('UP')
  const [amount, setAmount] = useState(10)
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const wsRef = useRef<WebSocket | null>(null)

  const loadMarkets = useCallback(async (s: string) => {
    const res = await api.markets(s, 40)
    setMarkets(res.markets)
    if (res.markets.length && !marketId) {
      setMarketId(res.markets[0].market_id)
    }
  }, [marketId])

  useEffect(() => {
    loadMarkets(split).catch((e) => setError(String(e)))
  }, [split, loadMarkets])

  useEffect(() => {
    if (!marketId) return
    setError(null)
    Promise.all([api.market(marketId, split), api.neighbors(marketId, split), api.book(marketId)])
      .then(([d, n, b]) => {
        setDetail(d)
        setNeighbors({ prev: n.prev, next: n.next })
        setBook(b)
        setSeriesLive(d.series.map((p) => ({ t: p.t, up: p.up ?? 0, down: p.down ?? 0, btc: p.btc })))
        setTick(null)
        setActivity([])
        setPlaying(false)
      })
      .catch((e) => setError(String(e)))
  }, [marketId, split])

  const stopWs = () => {
    wsRef.current?.close()
    wsRef.current = null
    setPlaying(false)
  }

  const startReplay = async () => {
    if (!marketId) return
    stopWs()
    setActivity([])
    setSeriesLive([])
    setError(null)

    let sid: string | null = null
    if (mode === 'paper') {
      const sess = await api.paperSession({
        market_id: marketId,
        split,
        strategy,
        speed,
        starting_cash: 1000,
        params: { threshold: 0.05, size_usd: 10, once_per_market: true },
      })
      sid = sess.session_id
      setSessionId(sid)
    }

    const ws = new WebSocket(wsUrl('/api/ws/replay'))
    wsRef.current = ws
    ws.onopen = () => {
      setPlaying(true)
      ws.send(
        JSON.stringify({
          market_id: marketId,
          split,
          strategy: mode === 'monitor' ? 'none' : strategy,
          speed,
          paper: mode === 'paper',
          session_id: sid,
          starting_cash: 1000,
          params: { threshold: 0.05, size_usd: 10, once_per_market: true },
        }),
      )
    }
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data)
      if (msg.type === 'error') {
        setError(msg.message)
        stopWs()
        return
      }
      if (msg.type === 'session' && msg.session_id) {
        setSessionId(msg.session_id)
      }
      if (msg.type === 'tick' || msg.type === 'tick_end') {
        const t = msg as Tick
        setTick(t)
        setSeriesLive((prev) => [
          ...prev,
          { t: t.timestamp, up: t.up_price, down: t.down_price, btc: t.btc_price },
        ])
        if (t.fills?.length) {
          setActivity((a) => [...t.fills!, ...a].slice(0, 100))
        }
        if (t.type === 'tick_end') {
          setPlaying(false)
        }
      }
      if (msg.type === 'done') {
        setPlaying(false)
      }
    }
    ws.onerror = () => setError('WebSocket error')
    ws.onclose = () => setPlaying(false)
  }

  useEffect(() => () => stopWs(), [])

  const up = tick?.up_price ?? detail?.first.up_price ?? 0.5
  const down = tick?.down_price ?? detail?.first.down_price ?? 0.5
  const btc = tick?.btc_price ?? detail?.first.btc_price
  const beat = tick?.btc_open ?? detail?.btc_open_price
  const remaining = tick?.remaining_seconds

  const chartData = useMemo(() => {
    if (seriesLive.length) return seriesLive
    return detail?.series ?? []
  }, [seriesLive, detail])

  const onTrade = () => {
    if (mode !== 'paper') return
    const ws = wsRef.current
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'order', side, action: 'BUY', size_usd: amount }))
      return
    }
    if (sessionId) {
      api.paperOrder({ session_id: sessionId, side, action: 'BUY', size_usd: amount }).catch((e) =>
        setError(String(e)),
      )
    }
  }

  const upBook = (book?.up as { bids?: { price: number; size: number }[]; asks?: { price: number; size: number }[] }) || {}

  return (
    <div>
      <div className="controls">
        <div>
          <label className="muted">Split</label>
          <select value={split} onChange={(e) => { setMarketId(''); setSplit(e.target.value) }}>
            <option value="validation">validation</option>
            <option value="test">test</option>
            <option value="train">train</option>
          </select>
        </div>
        <div style={{ minWidth: 180 }}>
          <label className="muted">Market</label>
          <select value={marketId} onChange={(e) => setMarketId(e.target.value)}>
            {markets.map((m) => (
              <option key={m.market_id} value={m.market_id}>
                {m.market_id}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label className="muted">Speed</label>
          <select value={speed} onChange={(e) => setSpeed(Number(e.target.value))}>
            <option value={5}>5x</option>
            <option value={10}>10x</option>
            <option value={30}>30x</option>
            <option value={60}>60x</option>
            <option value={120}>120x</option>
          </select>
        </div>
        {mode === 'paper' && (
          <div>
            <label className="muted">Strategy</label>
            <select value={strategy} onChange={(e) => setStrategy(e.target.value)}>
              <option value="none">none (manual)</option>
              <option value="lgbm_edge">lgbm_edge</option>
              <option value="edge_threshold">edge_threshold</option>
            </select>
          </div>
        )}
        <button type="button" onClick={() => startReplay()} disabled={!marketId || playing}>
          {playing ? 'Replaying…' : mode === 'paper' ? 'Start paper' : 'Replay'}
        </button>
        <button type="button" onClick={stopWs} disabled={!playing}>
          Stop
        </button>
      </div>

      {error && <p className="error">{error}</p>}

      <div className="market-header">
        <div className="market-title">
          <h1>Bitcoin Up or Down</h1>
          <div className="sub">
            {detail ? formatWindow(detail.start_time, detail.end_time) : '—'} · 5 minutes
            {remaining != null && (
              <>
                {' '}
                · <span className="countdown">{Math.max(0, remaining).toFixed(0)}s left</span>
              </>
            )}
          </div>
        </div>
        <div className="window-strip">
          <button type="button" disabled={!neighbors.prev} onClick={() => neighbors.prev && setMarketId(neighbors.prev)}>
            ← Prev
          </button>
          <span className="muted">{marketId || '—'}</span>
          <button type="button" disabled={!neighbors.next} onClick={() => neighbors.next && setMarketId(neighbors.next)}>
            Next →
          </button>
        </div>
      </div>

      <div className="price-beat-row">
        <div className="stat-card">
          <div className="label">Price to beat</div>
          <div className="value">{formatUsd(beat, 2)}</div>
        </div>
        <div className="stat-card">
          <div className="label">Bitcoin price</div>
          <div className={`value ${btc != null && beat != null ? (btc >= beat ? 'up' : 'down') : ''}`}>
            {formatUsd(btc, 2)}
          </div>
        </div>
      </div>

      <div className="layout-2">
        <div className="panel">
          <div className="outcome-row">
            <div className="outcome up">
              <div className="name">Up</div>
              <div className="pct">{formatPct(up)}</div>
            </div>
            <div className="outcome down">
              <div className="name">Down</div>
              <div className="pct">{formatPct(down)}</div>
            </div>
          </div>
          <PriceChart data={chartData} />
        </div>

        <div>
          <TradePanel
            side={side}
            onSide={setSide}
            amount={amount}
            onAmount={setAmount}
            onTrade={onTrade}
            disabled={mode !== 'paper' || (!playing && !sessionId)}
            upPrice={up}
            downPrice={down}
            cash={tick?.portfolio?.cash}
            modelPUp={tick?.model_p_up}
          />
          {mode === 'monitor' && (
            <p className="muted" style={{ marginTop: '0.75rem', fontSize: '0.85rem' }}>
              Monitor mode is view-only replay. Use Paper to place simulated trades.
            </p>
          )}
        </div>
      </div>

      <div className="tabs">
        {(['activity', 'book', 'positions', 'rules'] as const).map((t) => (
          <button key={t} type="button" className={tab === t ? 'active' : ''} onClick={() => setTab(t)}>
            {t[0].toUpperCase() + t.slice(1)}
          </button>
        ))}
      </div>

      <div className="panel">
        {tab === 'activity' && (
          <ul className="activity-list">
            {activity.length === 0 && <li className="muted">No fills yet</li>}
            {activity.map((f, i) => (
              <li key={i}>
                <span>
                  <strong>{f.source}</strong> {f.action} {f.side}
                </span>
                <span>
                  {f.shares.toFixed(2)} @ {formatUsd(f.price, 3)} {f.reason ? `· ${f.reason}` : ''}
                </span>
              </li>
            ))}
          </ul>
        )}
        {tab === 'book' && (
          <div className="book-grid">
            <div>
              <div className="muted">Asks</div>
              {(upBook.asks || []).slice(0, 8).map((l, i) => (
                <div className="book-row ask" key={`a${i}`}>
                  <span>{formatUsd(l.price, 3)}</span>
                  <span>{l.size.toFixed(0)}</span>
                </div>
              ))}
            </div>
            <div>
              <div className="muted">Bids</div>
              {(upBook.bids || []).slice(0, 8).map((l, i) => (
                <div className="book-row bid" key={`b${i}`}>
                  <span>{formatUsd(l.price, 3)}</span>
                  <span>{l.size.toFixed(0)}</span>
                </div>
              ))}
            </div>
          </div>
        )}
        {tab === 'positions' && (
          <div>
            <p>
              Cash: <strong>{formatUsd(tick?.portfolio?.cash ?? 1000)}</strong>
            </p>
            <p>
              Up shares: <strong>{(tick?.portfolio?.up_shares ?? 0).toFixed(2)}</strong>
            </p>
            <p>
              Down shares: <strong>{(tick?.portfolio?.down_shares ?? 0).toFixed(2)}</strong>
            </p>
            <p>
              Equity: <strong>{formatUsd(tick?.equity)}</strong>
            </p>
            {tick?.settlement && (
              <p className="success">
                Settled — winner {tick.settlement.winner === 1 ? 'UP' : 'DOWN'}, cash{' '}
                {formatUsd(tick.settlement.ending_cash)}
              </p>
            )}
          </div>
        )}
        {tab === 'rules' && (
          <div className="muted" style={{ fontSize: '0.9rem' }}>
            <p>
              This market resolves to <strong>Up</strong> if the Chainlink BTC/USD price at the end of the
              window is greater than or equal to the Price to Beat at the start; otherwise <strong>Down</strong>.
            </p>
            <p>
              poly-monitor v1 replays historical `fetch_real` data only. Paper fills are simulated at the
              displayed outcome price; no live orders are sent.
            </p>
          </div>
        )}
      </div>
    </div>
  )
}
