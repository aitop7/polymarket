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
import TradeSidebar from '../components/TradeSidebar'

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
  market_id?: string | null
  start_time?: number | null
  end_time?: number | null
  price_to_beat?: number | null
  book?: BookPayload
  live?: boolean
  error?: string
  message?: string
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
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [indexing, setIndexing] = useState(false)
  const [paused, setPaused] = useState(false)
  const [liveActive, setLiveActive] = useState(false)
  const [liveMarketId, setLiveMarketId] = useState<string>('')
  const [liveWindow, setLiveWindow] = useState<{ start: number; end: number } | null>(null)
  const [liveInterval, setLiveInterval] = useState(0.5)
  const wsRef = useRef<WebSocket | null>(null)
  const liveWsRef = useRef<WebSocket | null>(null)

  useEffect(() => {
    if (liveActive) return
    let cancelled = false
    setIndexing(true)
    setError(null)
    setMarkets([])
    setMarketId('')
    setSelectedTime('')
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
  }, [split, liveActive])

  useEffect(() => {
    if (liveActive || !selectedDate) return
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
  }, [split, selectedDate, liveActive])

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
    if (liveActive || !marketId) return
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
  }, [marketId, split, liveActive])

  const stopWs = () => {
    wsRef.current?.close()
    wsRef.current = null
    setPlaying(false)
    setPaused(false)
    if (detail && !liveActive) {
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
    if (!marketId || liveActive) return
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

  const startLive = () => {
    stopWs()
    liveWsRef.current?.close()
    liveWsRef.current = null
    setLiveActive(true)
    setSeriesLive([])
    setTick(null)
    setActivity([])
    setBook(null)
    setError(null)
    setLiveMarketId('')
    setLiveWindow(null)

    const interval = Math.max(0.1, Math.min(2, liveInterval))
    const ws = new WebSocket(wsUrl('/api/ws/live'))
    liveWsRef.current = ws
    ws.onopen = () => {
      ws.send(JSON.stringify({ interval_s: interval }))
    }
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data) as Tick
      if (msg.type === 'error') {
        setError(msg.message || 'Live feed error')
        return
      }
      if (msg.type === 'market') {
        if (msg.market_id) setLiveMarketId(String(msg.market_id))
        if (msg.start_time != null && msg.end_time != null) {
          setLiveWindow({ start: Number(msg.start_time), end: Number(msg.end_time) })
        }
        setSeriesLive([])
        return
      }
      if (msg.type === 'tick') {
        if (msg.error) setError(String(msg.error))
        setTick(msg)
        if (msg.market_id) setLiveMarketId(String(msg.market_id))
        if (msg.start_time != null && msg.end_time != null) {
          setLiveWindow({ start: Number(msg.start_time), end: Number(msg.end_time) })
        }
        if (msg.book) setBook(msg.book as BookPayload)
        if (msg.up_price != null && msg.down_price != null) {
          setSeriesLive((prev) => {
            const point = {
              t: msg.timestamp,
              up: msg.up_price!,
              down: msg.down_price!,
              btc: msg.btc_price ?? null,
            }
            const next = [...prev, point]
            return next.length > 600 ? next.slice(-600) : next
          })
        }
      }
    }
    ws.onerror = () => setError('Live WebSocket error')
  }

  const onLiveInterval = (s: number) => {
    const v = Math.max(0.1, Math.min(2, s))
    setLiveInterval(v)
    const ws = liveWsRef.current
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'interval', interval_s: v }))
    }
  }

  const toggleLive = () => {
    if (liveActive) {
      liveWsRef.current?.close()
      liveWsRef.current = null
      setLiveActive(false)
      setLiveMarketId('')
      setLiveWindow(null)
      if (detail) {
        setSeriesLive(
          detail.series.map((p) => ({ t: p.t, up: p.up ?? 0, down: p.down ?? 0, btc: p.btc })),
        )
        setTick(null)
        api.book(detail.market_id).then((b) => setBook(b as BookPayload)).catch(() => {})
      }
      return
    }
    startLive()
  }

  useEffect(
    () => () => {
      wsRef.current?.close()
      liveWsRef.current?.close()
    },
    [],
  )

  const displayMarketId = liveActive ? liveMarketId || '—' : marketId || detail?.market_id
  const windowLabel = liveActive
    ? liveWindow
      ? formatWindowEt(liveWindow.start, liveWindow.end)
      : 'Live market…'
    : detail
      ? formatWindowEt(detail.start_time, detail.end_time)
      : '—'

  const up = tick?.up_price ?? detail?.first.up_price ?? 0.5
  const down = tick?.down_price ?? detail?.first.down_price ?? 0.5
  const btc = tick?.btc_price ?? detail?.first.btc_price
  const beat = liveActive
    ? tick?.price_to_beat ?? tick?.btc_open ?? null
    : tick?.btc_open ?? detail?.btc_open_price
  const remaining = tick?.remaining_seconds

  const chartData = useMemo(() => {
    if (seriesLive.length) return seriesLive
    return detail?.series ?? []
  }, [seriesLive, detail])

  const liveLabel = liveActive
    ? liveMarketId
      ? `LIVE · ${liveMarketId}${remaining != null ? ` · ${Math.max(0, Math.floor(remaining))}s` : ''}`
      : 'LIVE · connecting…'
    : undefined

  const onTrade = (opts: { size_usd?: number; shares?: number }) => {
    if (mode !== 'paper' || liveActive) return
    const payload = {
      type: 'order',
      side,
      action: tradeAction,
      size_usd: opts.shares != null ? null : (opts.size_usd ?? 10),
      shares: opts.shares ?? null,
    }
    const ws = wsRef.current
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(payload))
      return
    }
    if (sessionId) {
      api
        .paperOrder({
          session_id: sessionId,
          side,
          action: tradeAction,
          size_usd: opts.shares != null ? null : (opts.size_usd ?? 10),
          shares: opts.shares ?? null,
        })
        .catch((e) => setError(String(e)))
    }
  }

  return (
    <div className="workspace">
      <ControlSidebar
        mode={mode}
        liveActive={liveActive}
        onToggleLive={toggleLive}
        liveLabel={liveLabel}
        liveInterval={liveInterval}
        onLiveInterval={onLiveInterval}
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
      />

      <div className="workspace-main">
        {error && <p className="error">{error}</p>}

        <BtcPricePanel
          marketId={displayMarketId}
          windowLabel={windowLabel}
          priceToBeat={beat}
          currentPrice={btc}
          remainingSeconds={
            remaining ??
            (liveActive
              ? null
              : detail
                ? (detail.end_time - detail.start_time) / 1000
                : null)
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
              {activity.length === 0 && (
                <li className="muted">{liveActive ? 'Live view — no fills' : 'No fills yet'}</li>
              )}
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
                {liveActive
                  ? 'Live mode shows the current CLOB ladder and Binance BTC. View only — no orders are sent.'
                  : 'Order book depth uses share buckets mapped to absolute price ranges from distance-from-traded-price storage bands.'}
              </p>
              <p>
                Historical replay and paper fills are simulated. Live trading view does not place orders.
              </p>
            </div>
          )}
        </div>
      </div>

      <TradeSidebar
        mode={mode}
        tradeAction={tradeAction}
        onTradeAction={setTradeAction}
        side={side}
        onSide={setSide}
        onTrade={onTrade}
        upPrice={up}
        downPrice={down}
        cash={tick?.portfolio?.cash}
        heldShares={
          side === 'UP' ? tick?.portfolio?.up_shares ?? 0 : tick?.portfolio?.down_shares ?? 0
        }
        tradeDisabled={liveActive || mode !== 'paper' || (!playing && !sessionId)}
        monitorHint={liveActive || mode === 'monitor'}
      />
    </div>
  )
}
