import { useEffect, useMemo, useRef, useState } from 'react'
import {
  api,
  formatUsd,
  formatWindowEt,
  type MarketDetail,
  type MarketSummary,
  wsUrl,
} from '../api'
import BtcPricePanel from '../components/BtcPricePanel'
import ControlSidebar from '../components/ControlSidebar'
import OrderBookPanel, { type BookPayload } from '../components/OrderBookPanel'
import PriceChart from '../components/PriceChart'

type Tick = {
  type: string
  index?: number
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

function formatSlotLabel(timeEt: string, startMs?: number, endMs?: number): string {
  // timeEt is HH:MM 24h ET — show 12h label when we have ms
  if (startMs != null && endMs != null) {
    const s = new Date(startMs)
    const e = new Date(endMs)
    const opts: Intl.DateTimeFormatOptions = {
      timeZone: 'America/New_York',
      hour: 'numeric',
      minute: '2-digit',
      hour12: true,
    }
    const a = new Intl.DateTimeFormat('en-US', opts).format(s)
    const b = new Intl.DateTimeFormat('en-US', opts).format(e)
    return `${a} – ${b} ET`
  }
  return `${timeEt} ET`
}

export default function MarketPage({ mode }: Props) {
  const [split, setSplit] = useState('validation')
  const [dateMin, setDateMin] = useState('')
  const [dateMax, setDateMax] = useState('')
  const [selectedDate, setSelectedDate] = useState('')
  const [selectedTime, setSelectedTime] = useState('')
  const [markets, setMarkets] = useState<MarketSummary[]>([])
  const [marketId, setMarketId] = useState<string>('')
  const [detail, setDetail] = useState<MarketDetail | null>(null)
  const [neighbors, setNeighbors] = useState<{ prev: string | null; next: string | null }>({
    prev: null,
    next: null,
  })
  const [speed, setSpeed] = useState(1)
  const [strategy, setStrategy] = useState(mode === 'paper' ? 'lgbm_edge' : 'none')
  const [playing, setPlaying] = useState(false)
  const [tick, setTick] = useState<Tick | null>(null)
  const [seriesLive, setSeriesLive] = useState<{ t: number; up: number; down: number; btc: number | null }[]>(
    [],
  )
  const [activity, setActivity] = useState<FillRow[]>([])
  const [tab, setTab] = useState<'activity' | 'positions' | 'rules'>('activity')
  const [book, setBook] = useState<BookPayload | null>(null)
  const [side, setSide] = useState<'UP' | 'DOWN'>('UP')
  const [tradeAction, setTradeAction] = useState<'BUY' | 'SELL'>('BUY')
  const [amount, setAmount] = useState(10)
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [indexing, setIndexing] = useState(false)
  const [paused, setPaused] = useState(false)
  const wsRef = useRef<WebSocket | null>(null)

  // Load available dates when split changes
  useEffect(() => {
    let cancelled = false
    setIndexing(true)
    setError(null)
    setMarkets([])
    setMarketId('')
    setSelectedTime('')
    // keep selectedDate visible until new range arrives (avoids empty disabled picker)
    api
      .marketDates(split)
      .then((res) => {
        if (cancelled) return
        setDateMin(res.min || '')
        setDateMax(res.max || '')
        const next =
          (selectedDate && res.dates.includes(selectedDate) && selectedDate) ||
          res.max ||
          res.dates[res.dates.length - 1] ||
          ''
        setSelectedDate(next)
      })
      .catch((e) => {
        if (!cancelled) setError(String(e))
      })
      .finally(() => {
        if (!cancelled) setIndexing(false)
      })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [split])

  // Load 5m slots for selected date
  useEffect(() => {
    if (!selectedDate) return
    let cancelled = false
    api
      .markets(split, { date: selectedDate })
      .then((res) => {
        if (cancelled) return
        setMarkets(res.markets)
        if (!res.markets.length) {
          setSelectedTime('')
          setMarketId('')
          return
        }
        setMarketId((prev) => {
          const keep = res.markets.find((m) => m.market_id === prev)
          if (keep) {
            setSelectedTime(keep.time_et || '')
            return prev
          }
          const still = res.markets.find((m) => (m.time_et || '') === selectedTime)
          const pick = still || res.markets[0]
          setSelectedTime(pick.time_et || '')
          return pick.market_id
        })
      })
      .catch((e) => {
        if (!cancelled) setError(String(e))
      })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [split, selectedDate])

  const onTimeChange = (timeEt: string) => {
    setSelectedTime(timeEt)
    const m = markets.find((x) => x.time_et === timeEt)
    if (m) {
      setMarketId(m.market_id)
      return
    }
    api.marketAt(split, { date: selectedDate, time: timeEt }).then((hit) => {
      setMarketId(hit.market_id)
      setSelectedTime(hit.time_et || timeEt)
    }).catch((e) => setError(String(e)))
  }

  useEffect(() => {
    if (!marketId) return
    setError(null)
    Promise.all([api.market(marketId, split), api.neighbors(marketId, split), api.book(marketId)])
      .then(([d, n, b]) => {
        setDetail(d)
        setNeighbors({ prev: n.prev, next: n.next })
        setBook(b as BookPayload)
        setSeriesLive(d.series.map((p) => ({ t: p.t, up: p.up ?? 0, down: p.down ?? 0, btc: p.btc })))
        setTick(null)
        setActivity([])
        setPlaying(false)
        // sync pickers from loaded market
        if (d.start_time) {
          const etDate = new Intl.DateTimeFormat('en-CA', {
            timeZone: 'America/New_York',
            year: 'numeric',
            month: '2-digit',
            day: '2-digit',
          }).format(new Date(d.start_time))
          const etTime = new Intl.DateTimeFormat('en-GB', {
            timeZone: 'America/New_York',
            hour: '2-digit',
            minute: '2-digit',
            hour12: false,
          }).format(new Date(d.start_time))
          setSelectedDate(etDate)
          setSelectedTime(etTime)
        }
      })
      .catch((e) => setError(String(e)))
  }, [marketId, split])

  const stopWs = () => {
    wsRef.current?.close()
    wsRef.current = null
    setPlaying(false)
    setPaused(false)
    if (detail) {
      setSeriesLive(
        detail.series.map((p) => ({ t: p.t, up: p.up ?? 0, down: p.down ?? 0, btc: p.btc })),
      )
      setTick(null)
    }
  }

  const pauseReplay = () => {
    const ws = wsRef.current
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'pause' }))
      setPaused(true)
    }
  }

  const resumeReplay = () => {
    const ws = wsRef.current
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'resume' }))
      setPaused(false)
    }
  }

  const setReplaySpeed = (n: number) => {
    setSpeed(n)
    const ws = wsRef.current
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'speed', speed: n }))
    }
  }

  const startReplay = async () => {
    if (!marketId) return
    stopWs()
    setActivity([])
    setSeriesLive([])
    setError(null)
    setPaused(false)

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
      setPaused(false)
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
        if (t.type === 'tick_end' || (t.index != null && t.index % 5 === 0)) {
          api.book(marketId, t.timestamp).then((b) => setBook(b as BookPayload)).catch(() => {})
        }
        if (t.type === 'tick_end') {
          setPlaying(false)
          setPaused(false)
        }
      }
      if (msg.type === 'done') {
        setPlaying(false)
        setPaused(false)
      }
    }
    ws.onerror = () => setError('WebSocket error')
    ws.onclose = () => {
      setPlaying(false)
      setPaused(false)
    }
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
      ws.send(JSON.stringify({ type: 'order', side, action: tradeAction, size_usd: amount }))
      return
    }
    if (sessionId) {
      api
        .paperOrder({ session_id: sessionId, side, action: tradeAction, size_usd: amount })
        .catch((e) => setError(String(e)))
    }
  }

  return (
    <div className="workspace">
      <div className="workspace-main">
        {error && <p className="error">{error}</p>}

        <BtcPricePanel
          marketId={marketId || detail?.market_id}
          windowLabel={detail ? formatWindowEt(detail.start_time, detail.end_time) : '—'}
          priceToBeat={beat}
          currentPrice={btc}
          remainingSeconds={
            remaining ?? (detail ? (detail.end_time - detail.start_time) / 1000 : null)
          }
        />

        <div className="panel">
          <PriceChart data={chartData} priceToBeat={beat} mode="btc" title="Bitcoin price" />
          <PriceChart data={chartData} mode="outcomes" title="Up / Down price" />
        </div>

        <OrderBookPanel book={book} />

        <div className="tabs">
          {(['activity', 'positions', 'rules'] as const).map((t) => (
            <button
              key={t}
              type="button"
              className={tab === t ? 'active' : ''}
              onClick={() => setTab(t)}
            >
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
                This market resolves to <strong>Up</strong> if the Chainlink BTC/USD price at the end of
                the window is greater than or equal to the Price to Beat at the start; otherwise{' '}
                <strong>Down</strong>.
              </p>
              <p>
                Order book depth uses share buckets mapped to <strong>absolute price ranges</strong>,
                derived from distance-from-traded-price storage bands.
              </p>
              <p>
                poly-monitor v1 replays historical data only. Paper fills are simulated; no live orders
                are sent.
              </p>
            </div>
          )}
        </div>
      </div>

      <ControlSidebar
        mode={mode}
        split={split}
        onSplit={setSplit}
        indexing={indexing}
        dateMin={dateMin}
        dateMax={dateMax}
        selectedDate={selectedDate}
        onDate={setSelectedDate}
        selectedTime={selectedTime}
        markets={markets}
        onTime={onTimeChange}
        formatSlotLabel={formatSlotLabel}
        speed={speed}
        onSpeed={setReplaySpeed}
        playing={playing}
        paused={paused}
        onPlay={() => startReplay()}
        onPause={pauseReplay}
        onResume={resumeReplay}
        onStop={stopWs}
        marketId={marketId}
        hasPrev={Boolean(neighbors.prev)}
        hasNext={Boolean(neighbors.next)}
        onPrev={() => neighbors.prev && setMarketId(neighbors.prev)}
        onNext={() => neighbors.next && setMarketId(neighbors.next)}
        strategy={strategy}
        onStrategy={setStrategy}
        tradeAction={tradeAction}
        onTradeAction={setTradeAction}
        side={side}
        onSide={setSide}
        amount={amount}
        onAmount={setAmount}
        onTrade={onTrade}
        upPrice={up}
        downPrice={down}
        cash={tick?.portfolio?.cash}
        modelPUp={tick?.model_p_up}
        tradeDisabled={mode !== 'paper' || (!playing && !sessionId)}
      />
    </div>
  )
}
