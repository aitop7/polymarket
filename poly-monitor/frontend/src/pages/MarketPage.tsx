import { useEffect, useMemo, useRef, useState } from 'react'
import {
  api,
  formatUsd,
  formatWindowEt,
  type HolderRow,
  type LiveHoldersResponse,
  type LiveSeriesPoint,
  type MarketDetail,
  type MarketSummary,
  wsUrl,
} from '../api'
import BtcPricePanel from '../components/BtcPricePanel'
import ControlSidebar from '../components/ControlSidebar'
import OrderBookPanel, { type BookPayload } from '../components/OrderBookPanel'
import PriceChart, { type BtcSeriesVisibility, type TimeDomain } from '../components/PriceChart'
import TradeSidebar from '../components/TradeSidebar'

const DEFAULT_X_SPAN_MS = 180_000

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
  condition_id?: string | null
  start_time?: number | null
  end_time?: number | null
  price_to_beat?: number | null
  btc_twap_30s?: number | null
  btc_twap_ts?: number | null
  btc_twap_error?: string | null
  btc_chainlink?: number | null
  btc_chainlink_ts?: number | null
  book?: BookPayload
  live?: boolean
  error?: string
  message?: string
  updated_at?: number
  up?: HolderRow[]
  down?: HolderRow[]
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

function sortHolders(rows: HolderRow[] | undefined): HolderRow[] {
  return [...(rows ?? [])].sort((a, b) => {
    const da = Number(b.amount) - Number(a.amount)
    if (da !== 0) return da
    return String(a.proxy_wallet).localeCompare(String(b.proxy_wallet))
  })
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
  const [collection, setCollection] = useState<'before_twap' | 'twap'>('twap')
  const effectiveSplit = collection === 'twap' ? 'twap' : split
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
  const [seriesLive, setSeriesLive] = useState<LiveSeriesPoint[]>([])
  const [activity, setActivity] = useState<FillRow[]>([])
  const [tab, setTab] = useState<'activity' | 'positions' | 'rules'>('activity')
  const [book, setBook] = useState<BookPayload | null>(null)
  const [side, setSide] = useState<'UP' | 'DOWN'>('UP')
  const [tradeAction, setTradeAction] = useState<'BUY' | 'SELL'>('BUY')
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [indexing, setIndexing] = useState(false)
  const [paused, setPaused] = useState(false)
  const [liveActive, setLiveActive] = useState(true)
  const [liveMarketId, setLiveMarketId] = useState<string>('')
  const [liveWindow, setLiveWindow] = useState<{ start: number; end: number } | null>(null)
  const [btcSeriesVisible, setBtcSeriesVisible] = useState<BtcSeriesVisibility>({
    twap: true,
    chainlink: false,
    binance: false,
  })
  const [sharedHoverTime, setSharedHoverTime] = useState<number | null>(null)
  const [chartXDomain, setChartXDomain] = useState<TimeDomain | null>(null)
  const [followLiveX, setFollowLiveX] = useState(true)
  const [nowMs, setNowMs] = useState(() => Date.now())
  const [liveInterval, setLiveInterval] = useState(0.5)
  const [holders, setHolders] = useState<LiveHoldersResponse | null>(null)
  const [holdersRevision, setHoldersRevision] = useState(0)
  const [holdersReloading, setHoldersReloading] = useState(false)
  const wsRef = useRef<WebSocket | null>(null)
  const liveWsRef = useRef<WebSocket | null>(null)
  const liveActiveRef = useRef(liveActive)
  const liveReconnectTimer = useRef<number | null>(null)
  const liveReconnectAttempt = useRef(0)
  liveActiveRef.current = liveActive

  const clearLiveReconnectTimer = () => {
    if (liveReconnectTimer.current != null) {
      window.clearTimeout(liveReconnectTimer.current)
      liveReconnectTimer.current = null
    }
  }

  const scheduleLiveReconnect = (_reason: string) => {
    if (!liveActiveRef.current) return
    if (liveReconnectTimer.current != null) return
    liveReconnectAttempt.current += 1
    const attempt = liveReconnectAttempt.current
    const delay = Math.min(10_000, Math.round(500 * 1.6 ** Math.min(attempt - 1, 8)))
    liveReconnectTimer.current = window.setTimeout(() => {
      liveReconnectTimer.current = null
      if (liveActiveRef.current && !liveWsRef.current) startLive({ soft: true })
    }, delay)
  }

  useEffect(() => {
    if (liveActive) return
    let cancelled = false
    setIndexing(true)
    setError(null)
    setMarkets([])
    setMarketId('')
    setSelectedTime('')
    api
      .marketDates(effectiveSplit)
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
  }, [effectiveSplit, liveActive])

  useEffect(() => {
    if (liveActive || !selectedDate) return
    let cancelled = false
    api
      .markets(effectiveSplit, { date: selectedDate })
      .then((res) => {
        if (cancelled) return
        if (!res.markets.length) {
          setMarkets([])
          setSelectedTime('')
          setMarketId('')
          return
        }
        const ordered = [...res.markets].sort(
          (a, b) => (b.start_time || 0) - (a.start_time || 0),
        )
        setMarkets(ordered)
        setMarketId((prev) => {
          const keep = ordered.find((m) => m.market_id === prev)
          if (keep) {
            setSelectedTime(keep.time_et || '')
            return prev
          }
          const still = ordered.find((m) => (m.time_et || '') === selectedTime)
          const pick = still || ordered[0]
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
  }, [effectiveSplit, selectedDate, liveActive])

  const onTimeChange = (timeEt: string) => {
    setSelectedTime(timeEt)
    const m = markets.find((x) => x.time_et === timeEt)
    if (m) {
      setMarketId(m.market_id)
      return
    }
    api.marketAt(effectiveSplit, { date: selectedDate, time: timeEt }).then((hit) => {
      setMarketId(hit.market_id)
      setSelectedTime(hit.time_et || timeEt)
    }).catch((e) => setError(String(e)))
  }

  useEffect(() => {
    if (liveActive || !marketId) return
    setError(null)
    Promise.all([api.market(marketId, effectiveSplit), api.neighbors(marketId, effectiveSplit), api.book(marketId)])
      .then(([d, n, b]) => {
        setDetail(d)
        setNeighbors({ prev: n.prev, next: n.next })
        setBook(b as BookPayload)
        setSeriesLive(
          d.series.map((p) => ({
            t: p.t,
            up: p.up ?? 0,
            down: p.down ?? 0,
            btc: p.btc,
            twap: p.twap ?? null,
            chainlink: p.chainlink ?? null,
          })),
        )
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
  }, [marketId, effectiveSplit, liveActive])

  const stopWs = () => {
    wsRef.current?.close()
    wsRef.current = null
    setPlaying(false)
    setPaused(false)
    if (detail && !liveActive) {
      setSeriesLive(
        detail.series.map((p) => ({
          t: p.t,
          up: p.up ?? 0,
          down: p.down ?? 0,
          btc: p.btc,
          twap: p.twap ?? null,
          chainlink: p.chainlink ?? null,
        })),
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
        split: effectiveSplit,
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
          split: effectiveSplit,
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
          {
            t: t.timestamp,
            up: t.up_price,
            down: t.down_price,
            btc: t.btc_price,
            twap: t.btc_twap_30s ?? t.btc_price ?? null,
            chainlink: t.btc_chainlink ?? null,
          },
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

  const startLive = (opts?: { soft?: boolean }) => {
    const soft = Boolean(opts?.soft)
    clearLiveReconnectTimer()
    stopWs()
    const prev = liveWsRef.current
    liveWsRef.current = null
    if (prev) {
      prev.onerror = null
      prev.onclose = null
      prev.onmessage = null
      prev.close()
    }
    setLiveActive(true)
    liveActiveRef.current = true
    if (!soft) {
      setSeriesLive([])
      setTick(null)
      setActivity([])
      setBook(null)
      setHolders(null)
      setHoldersRevision(0)
      setLiveMarketId('')
      setLiveWindow(null)
      liveReconnectAttempt.current = 0
    }
    setError(null)

    const interval = Math.max(0.1, Math.min(2, liveInterval))
    const ws = new WebSocket(wsUrl('/api/ws/live'))
    liveWsRef.current = ws

    const seedSeries = (marketId?: string | null) => {
      const reqId = marketId ? String(marketId) : ''
      api
        .liveSeries(reqId || undefined, 180_000)
        .then((res) => {
          if (liveWsRef.current !== ws) return
          if (reqId && res.market_id && String(res.market_id) !== reqId) return
          const points = (res.series ?? [])
            .filter((p) => p.t != null && Number.isFinite(Number(p.t)))
            .map((p) => ({
              t: Number(p.t),
              up: p.up ?? null,
              down: p.down ?? null,
              btc: p.btc ?? null,
              twap: p.twap ?? null,
              chainlink: p.chainlink ?? null,
            }))
          if (!points.length) return
          setSeriesLive((prev) => {
            // Prefer longer history; keep any newer live ticks past the seed.
            if (!prev.length) return points
            const seedLast = points[points.length - 1].t
            const newer = prev.filter((p) => p.t > seedLast)
            const byT = new Map<number, LiveSeriesPoint>()
            for (const p of points) byT.set(p.t, p)
            for (const p of newer) byT.set(p.t, p)
            return [...byT.values()].sort((a, b) => a.t - b.t)
          })
        })
        .catch(() => {
          /* backfill is best-effort */
        })
    }

    ws.onopen = () => {
      if (liveWsRef.current !== ws) return
      liveReconnectAttempt.current = 0
      setError(null)
      ws.send(JSON.stringify({ interval_s: interval }))
      // Seed immediately (buffer/parquet) before ticks accumulate.
      seedSeries()
    }
    ws.onmessage = (ev) => {
      if (liveWsRef.current !== ws) return
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
        // Clear prior window quotes so Price To Beat doesn't stick across markets.
        setTick(null)
        setSeriesLive([])
        setBook(null)
        setHolders(null)
        seedSeries(msg.market_id)
        return
      }
      if (msg.type === 'holders') {
        const upSorted = sortHolders(msg.up)
        const downSorted = sortHolders(msg.down)
        setHolders({
          market_id: msg.market_id ?? null,
          condition_id: msg.condition_id ?? null,
          updated_at: msg.updated_at ?? Date.now(),
          live: true,
          up: upSorted,
          down: downSorted,
        })
        setHoldersRevision((n) => n + 1)
        return
      }
      if (msg.type === 'tick') {
        // Soft feed notes (e.g. no active market) — don't sticky-banner WS errors.
        if (msg.error) setError(String(msg.error))
        else setError(null)
        setTick(msg)
        if (msg.market_id) setLiveMarketId(String(msg.market_id))
        if (msg.start_time != null && msg.end_time != null) {
          setLiveWindow({ start: Number(msg.start_time), end: Number(msg.end_time) })
        }
        if (msg.book) setBook(msg.book as BookPayload)
        if (msg.up_price != null && msg.down_price != null) {
          setSeriesLive((prev) => {
            let up: number | null = msg.up_price!
            let down: number | null = msg.down_price!
            // Until we have a real mid-market quote, ignore 1¢/99¢ open stubs.
            const hasRealOutcome = prev.some(
              (p) => p.up != null && p.up > 0.02 && p.up < 0.98,
            )
            if (!hasRealOutcome && (up <= 0.02 || up >= 0.98 || down <= 0.02 || down >= 0.98)) {
              up = null
              down = null
            }
            const point: LiveSeriesPoint = {
              t: msg.timestamp,
              up,
              down,
              btc: msg.btc_price ?? null,
              twap: msg.btc_twap_30s ?? null,
              chainlink: msg.btc_chainlink ?? null,
            }
            if (!prev.length) return [point]
            const last = prev[prev.length - 1]
            if (last.t === point.t) {
              const next = prev.slice(0, -1)
              next.push({ ...last, ...point })
              return next
            }
            if (point.t < last.t) {
              // Rare clock skew / late seed overlap — ignore older live tick.
              return prev
            }
            const next = [...prev, point]
            return next.length > 900 ? next.slice(-900) : next
          })
        }
      }
    }
    // Browsers fire onerror on intentional close/reconnect; ignore stale sockets.
    ws.onerror = () => {
      if (liveWsRef.current !== ws) return
    }
    ws.onclose = (ev) => {
      if (liveWsRef.current !== ws) return
      liveWsRef.current = null
      // Only surface unexpected drops (not clean client close / StrictMode teardown).
      if (!ev.wasClean && ev.code !== 1000 && ev.code !== 1001) {
        setError('Live WebSocket disconnected — reconnecting…')
      }
      // Auto-reconnect while live mode remains on (API reload / proxy blip).
      scheduleLiveReconnect(`close:${ev.code}`)
    }
  }

  // Live market is the default: connect on mount.
  useEffect(() => {
    startLive()
    return () => {
      clearLiveReconnectTimer()
      liveActiveRef.current = false
      const ws = liveWsRef.current
      liveWsRef.current = null
      if (ws) {
        ws.onerror = null
        ws.onclose = null
        ws.onmessage = null
        ws.close()
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const reloadHolders = async () => {
    if (holdersReloading) return
    setHoldersReloading(true)
    try {
      const res = await api.liveHolders(20)
      setHolders({
        market_id: res.market_id ?? null,
        condition_id: res.condition_id ?? null,
        updated_at: res.updated_at ?? Date.now(),
        live: true,
        up: sortHolders(res.up),
        down: sortHolders(res.down),
      })
      setHoldersRevision((n) => n + 1)
    } catch {
      /* keep last good snapshot */
    } finally {
      setHoldersReloading(false)
    }
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
      clearLiveReconnectTimer()
      liveActiveRef.current = false
      const ws = liveWsRef.current
      liveWsRef.current = null
      if (ws) {
        ws.onerror = null
        ws.onclose = null
        ws.onmessage = null
        ws.close()
      }
      setLiveActive(false)
      setLiveMarketId('')
      setLiveWindow(null)
      setHolders(null)
      setError(null)
      if (detail) {
        setSeriesLive(
          detail.series.map((p) => ({
            t: p.t,
            up: p.up ?? 0,
            down: p.down ?? 0,
            btc: p.btc,
            twap: p.twap ?? null,
            chainlink: p.chainlink ?? null,
          })),
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

  const finalHistoryPrice = useMemo(() => {
    const series = detail?.series
    if (!series?.length) return null
    for (let i = series.length - 1; i >= 0; i--) {
      const tw = series[i].twap
      if (tw != null && Number.isFinite(tw)) return tw
    }
    for (let i = series.length - 1; i >= 0; i--) {
      const btc = series[i].btc
      if (btc != null && Number.isFinite(btc)) return btc
    }
    return null
  }, [detail])

  const twap = liveActive
    ? tick?.btc_twap_30s ?? null
    : playing || paused
      ? tick?.btc_twap_30s ?? tick?.btc_price ?? null
      : finalHistoryPrice
  const beat = liveActive
    ? tick?.price_to_beat ?? tick?.btc_open ?? null
    : tick?.btc_open ?? detail?.btc_open_price
  const remaining = liveActive
    ? tick?.remaining_seconds
    : playing || paused
      ? tick?.remaining_seconds
      : null

  // Wall-clock tick so the live timeline scrolls smoothly between WS updates.
  useEffect(() => {
    if (!liveActive) return
    const id = window.setInterval(() => setNowMs(Date.now()), 100)
    return () => window.clearInterval(id)
  }, [liveActive])

  const chartData = useMemo(() => {
    if (seriesLive.length) return seriesLive
    return (detail?.series ?? []).map((p) => ({
      t: p.t,
      up: p.up ?? 0,
      down: p.down ?? 0,
      btc: p.btc,
      twap: p.twap ?? null,
      chainlink: p.chainlink ?? null,
    }))
  }, [seriesLive, detail])

  const xFullDomain = useMemo((): TimeDomain => {
    if (liveActive && liveWindow) {
      return [liveWindow.start, liveWindow.end]
    }
    if (detail?.start_time != null && detail?.end_time != null) {
      return [detail.start_time, detail.end_time]
    }
    if (chartData.length >= 2) {
      return [chartData[0].t, chartData[chartData.length - 1].t]
    }
    if (chartData.length === 1) {
      return [chartData[0].t, chartData[0].t + DEFAULT_X_SPAN_MS]
    }
    return [nowMs - DEFAULT_X_SPAN_MS, nowMs]
  }, [liveActive, liveWindow, detail, chartData, nowMs])

  // Live: trailing fixed-span window. Historical idle/paused: full 5m market.
  const xDefaultDomain = useMemo((): TimeDomain => {
    const [f0, f1] = xFullDomain
    if (!liveActive && (!playing || paused)) {
      const end = Number.isFinite(f1) && f1 > f0 ? f1 : f0 + 300_000
      const start = Number.isFinite(f0) ? f0 : end - 300_000
      return [start, end]
    }
    const latestData = chartData.length > 0 ? chartData[chartData.length - 1].t : f0
    const end = liveActive
      ? Math.min(f1, Math.max(latestData, nowMs))
      : Math.min(f1, Math.max(f0 + 1, latestData))
    const start = end - DEFAULT_X_SPAN_MS
    return [start, end]
  }, [xFullDomain, chartData, liveActive, nowMs, playing, paused])

  // When following live, always use the sliding default window.
  useEffect(() => {
    if (!followLiveX) return
    setChartXDomain((prev) => {
      if (
        prev != null &&
        Math.abs(prev[0] - xDefaultDomain[0]) < 1 &&
        Math.abs(prev[1] - xDefaultDomain[1]) < 1
      ) {
        return prev
      }
      return xDefaultDomain
    })
  }, [followLiveX, xDefaultDomain])

  // Re-arm default domain on market / mode / playback state changes.
  useEffect(() => {
    setFollowLiveX(true)
    setChartXDomain(null)
  }, [liveActive, liveWindow?.start, liveMarketId, marketId, playing, paused])

  const sharedXDomain = followLiveX ? xDefaultDomain : (chartXDomain ?? xDefaultDomain)

  const onChartXDomainChange = (next: TimeDomain) => {
    setFollowLiveX(false)
    setChartXDomain(next)
  }

  const onChartXDomainReset = () => {
    setFollowLiveX(true)
    setChartXDomain(xDefaultDomain)
  }

  const historyOutcome = useMemo((): 'Up' | 'Down' | null => {
    if (liveActive || playing) return null
    const w = tick?.settlement?.winner ?? detail?.winner
    if (w === 1) return 'Up'
    if (w === 0) return 'Down'
    if (finalHistoryPrice != null && beat != null) {
      return finalHistoryPrice >= beat ? 'Up' : 'Down'
    }
    return null
  }, [liveActive, playing, tick?.settlement?.winner, detail?.winner, finalHistoryPrice, beat])

  const outcomeSubtitle =
    detail != null
      ? `Bitcoin Up or Down - ${formatWindowEt(detail.start_time, detail.end_time)}`
      : windowLabel

  const showOutcomeCard = !liveActive && !playing && !paused

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
        collection={collection}
        onCollection={setCollection}
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
          twapPrice={twap}
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
          <PriceChart
            data={chartData}
            priceToBeat={beat}
            mode="btc"
            title="BTC price"
            seriesVisible={btcSeriesVisible}
            onSeriesVisibleChange={setBtcSeriesVisible}
            xDomain={sharedXDomain}
            onXDomainChange={onChartXDomainChange}
            onXDomainReset={onChartXDomainReset}
            xFullDomain={xFullDomain}
            xDefaultDomain={xDefaultDomain}
            hoverTime={sharedHoverTime}
            onHoverTimeChange={setSharedHoverTime}
          />
          <PriceChart
            data={chartData}
            mode="outcomes"
            title="Up / Down price"
            xDomain={sharedXDomain}
            onXDomainChange={onChartXDomainChange}
            onXDomainReset={onChartXDomainReset}
            xFullDomain={xFullDomain}
            xDefaultDomain={xDefaultDomain}
            hoverTime={sharedHoverTime}
            onHoverTimeChange={setSharedHoverTime}
          />
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
                This market resolves to <strong>Up</strong> if the Chainlink BTC/USD 30s TWAP at the end of
                the window is greater than or equal to the Price to Beat (30s TWAP at window start, set when
                the market opens); otherwise <strong>Down</strong>.
              </p>
              <p>
                {liveActive
                  ? 'Live mode shows the current CLOB ladder and Binance BTC. View only — no orders are sent.'
                  : 'Order book depth uses share buckets mapped to absolute price ranges from distance-from-traded-price storage bands.'}
              </p>
              <p>
                Historical replay and paper fills are simulated. Live market view does not place orders.
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
        upHasAsk={book == null ? true : (book.up?.asks?.length ?? 0) > 0}
        downHasAsk={book == null ? true : (book.down?.asks?.length ?? 0) > 0}
        upHasBid={book == null ? true : (book.up?.bids?.length ?? 0) > 0}
        downHasBid={book == null ? true : (book.down?.bids?.length ?? 0) > 0}
        cash={tick?.portfolio?.cash}
        heldShares={
          side === 'UP' ? tick?.portfolio?.up_shares ?? 0 : tick?.portfolio?.down_shares ?? 0
        }
        tradeDisabled={liveActive || mode !== 'paper' || (!playing && !sessionId)}
        monitorHint={liveActive || mode === 'monitor'}
        liveHolders={liveActive}
        holders={holders}
        holdersRevision={holdersRevision}
        onReloadHolders={reloadHolders}
        holdersReloading={holdersReloading}
        showOutcome={showOutcomeCard}
        outcome={historyOutcome}
        outcomeSubtitle={outcomeSubtitle}
      />
    </div>
  )
}
