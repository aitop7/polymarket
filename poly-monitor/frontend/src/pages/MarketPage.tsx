import { useEffect, useMemo, useRef, useState } from 'react'
import {
  api,
  formatUsd,
  formatWindowEt,
  type HolderRow,
  type LiveActivityTrade,
  type LiveHoldersResponse,
  type LiveSeriesPoint,
  type MarketDetail,
  type MarketSummary,
  wsUrl,
} from '../api'
import BtcPricePanel from '../components/BtcPricePanel'
import ControlSidebar from '../components/ControlSidebar'
import FeedSidebar from '../components/FeedSidebar'
import OrderBookPanel, { type BookPayload } from '../components/OrderBookPanel'
import PriceChart, { type BtcSeriesVisibility, type TimeDomain } from '../components/PriceChart'
import TradeSidebar from '../components/TradeSidebar'
import VolumeChart from '../components/VolumeChart'

const DEFAULT_X_SPAN_MS = 180_000

function volumeFields(p: {
  bn_buy?: number | null
  bn_sell?: number | null
  up_buy_vol?: number | null
  up_sell_vol?: number | null
  down_buy_vol?: number | null
  down_sell_vol?: number | null
}) {
  return {
    bn_buy: p.bn_buy ?? 0,
    bn_sell: p.bn_sell ?? 0,
    up_buy_vol: p.up_buy_vol ?? 0,
    up_sell_vol: p.up_sell_vol ?? 0,
    down_buy_vol: p.down_buy_vol ?? 0,
    down_sell_vol: p.down_sell_vol ?? 0,
  }
}

function maxVolumeFields(
  a: ReturnType<typeof volumeFields>,
  b: ReturnType<typeof volumeFields>,
) {
  return {
    bn_buy: Math.max(a.bn_buy, b.bn_buy),
    bn_sell: Math.max(a.bn_sell, b.bn_sell),
    up_buy_vol: Math.max(a.up_buy_vol, b.up_buy_vol),
    up_sell_vol: Math.max(a.up_sell_vol, b.up_sell_vol),
    down_buy_vol: Math.max(a.down_buy_vol, b.down_buy_vol),
    down_sell_vol: Math.max(a.down_sell_vol, b.down_sell_vol),
  }
}

const VOLUME_BUCKET_MS = 5_000

/** Bucket start for a timestamp; exact boundaries belong to the previous bucket. */
function volumeBucketStart(ts: number): number {
  const t = Math.floor(ts)
  let start = Math.floor(t / VOLUME_BUCKET_MS) * VOLUME_BUCKET_MS
  if (t === start && start > 0) start -= VOLUME_BUCKET_MS
  return start
}

/** Bump the forming 5s Up/Down volume bucket from a live activity trade. */
function applyActivityTradeToSeries(
  prev: LiveSeriesPoint[],
  trade: LiveActivityTrade,
): LiveSeriesPoint[] {
  const shares = Number(trade.shares) || 0
  if (shares <= 0) return prev
  const ts = Number(trade.timestamp) || Date.now()
  const start = volumeBucketStart(ts)
  const end = start + VOLUME_BUCKET_MS
  const isDown = trade.outcome === 'Down' || trade.token === true
  const isSell = trade.side === 'SELL' || trade.is_sell === true
  const key = isDown
    ? isSell
      ? 'down_sell_vol'
      : 'down_buy_vol'
    : isSell
      ? 'up_sell_vol'
      : 'up_buy_vol'

  const next = [...prev]
  let idx = -1
  for (let i = next.length - 1; i >= 0; i--) {
    const t = Number(next[i].t)
    // Same 5s bucket (include legacy points parked at bucket end).
    if (t > start && t <= end) {
      idx = i
      break
    }
    if (t === start) {
      idx = i
      break
    }
  }
  if (idx < 0) {
    // Park on bucket start so the bar stays inside the live x-domain (not in the future).
    const point: LiveSeriesPoint = {
      t: start,
      up: null,
      down: null,
      btc: null,
      twap: null,
      chainlink: null,
      ...volumeFields({}),
      [key]: shares,
    }
    next.push(point)
    next.sort((a, b) => a.t - b.t)
    return next.length > 1200 ? next.slice(-1200) : next
  }
  const cur = next[idx]
  const vols = volumeFields(cur)
  next[idx] = {
    ...cur,
    ...vols,
    [key]: (vols[key] || 0) + shares,
  }
  return next
}

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

const HOLDERS_TOUCH_MS = 45_000

function holderTouchKey(side: 'up' | 'down', wallet: string): string {
  return `${side}:${wallet.toLowerCase()}`
}

/** Instant holder bump from RTDS activity (Data API holder lists lag). */
function applyActivityTradeToHolders(
  prev: LiveHoldersResponse | null,
  trade: LiveActivityTrade,
  touched: Map<string, number>,
): LiveHoldersResponse {
  const shares = Number(trade.shares) || 0
  const wallet = String(trade.proxy_wallet || '').trim()
  const base: LiveHoldersResponse = prev ?? {
    market_id: null,
    condition_id: null,
    updated_at: Date.now(),
    live: true,
    up: [],
    down: [],
  }
  if (shares <= 0 || !wallet) return base

  const side: 'up' | 'down' =
    trade.outcome === 'Down' || trade.token === true ? 'down' : 'up'
  const isSell = trade.side === 'SELL' || trade.is_sell === true
  const key = wallet.toLowerCase()
  const rows = [...(side === 'up' ? base.up : base.down)]
  const idx = rows.findIndex((r) => r.proxy_wallet.toLowerCase() === key)
  const delta = isSell ? -shares : shares

  if (idx >= 0) {
    const nextAmt = Math.max(0, Number(rows[idx].amount) + delta)
    if (nextAmt <= 1e-9) rows.splice(idx, 1)
    else {
      rows[idx] = {
        ...rows[idx],
        amount: nextAmt,
        display_name: rows[idx].display_name || trade.name || wallet,
        profile_image: rows[idx].profile_image || trade.profile_image || undefined,
      }
    }
  } else if (!isSell) {
    rows.push({
      proxy_wallet: wallet,
      display_name:
        trade.name ||
        (wallet.length > 12 ? `${wallet.slice(0, 6)}...${wallet.slice(-4)}` : wallet),
      amount: shares,
      profile_image: trade.profile_image || undefined,
    })
  } else {
    return base
  }

  touched.set(holderTouchKey(side, wallet), Date.now())
  const sorted = sortHolders(rows).slice(0, 20)
  return {
    ...base,
    updated_at: Date.now(),
    live: true,
    up: side === 'up' ? sorted : base.up,
    down: side === 'down' ? sorted : base.down,
  }
}

/** Rebuild top holders from activity trades up to a playhead (history replay). */
function holdersFromActivity(
  trades: LiveActivityTrade[],
  playheadTs: number,
  meta: { market_id: string | null; condition_id: string | null },
): LiveHoldersResponse {
  const chron = [...trades]
    .filter((t) => Number(t.timestamp) <= playheadTs)
    .sort((a, b) => Number(a.timestamp) - Number(b.timestamp))
  let h: LiveHoldersResponse = {
    market_id: meta.market_id,
    condition_id: meta.condition_id,
    updated_at: playheadTs,
    live: true,
    up: [],
    down: [],
  }
  const touched = new Map<string, number>()
  for (const t of chron) {
    h = applyActivityTradeToHolders(h, t, touched)
  }
  return { ...h, live: true, updated_at: playheadTs }
}

/** Reconcile Data API snapshot without wiping recent RTDS bumps. */
function mergeHoldersFromApi(
  api: LiveHoldersResponse,
  prev: LiveHoldersResponse | null,
  touched: Map<string, number>,
): LiveHoldersResponse {
  const now = Date.now()
  const mergeSide = (
    side: 'up' | 'down',
    apiRows: HolderRow[],
    prevRows: HolderRow[],
  ): HolderRow[] => {
    const prevMap = new Map(prevRows.map((r) => [r.proxy_wallet.toLowerCase(), r]))
    const out = new Map<string, HolderRow>()
    for (const r of apiRows) {
      const k = r.proxy_wallet.toLowerCase()
      const local = prevMap.get(k)
      const touchedAt = touched.get(holderTouchKey(side, k)) ?? 0
      if (local && now - touchedAt < HOLDERS_TOUCH_MS) {
        out.set(k, {
          ...r,
          amount: local.amount,
          display_name: local.display_name || r.display_name,
          profile_image: local.profile_image || r.profile_image,
        })
      } else {
        out.set(k, r)
      }
    }
    for (const [k, local] of prevMap) {
      if (out.has(k)) continue
      const touchedAt = touched.get(holderTouchKey(side, k)) ?? 0
      if (now - touchedAt < HOLDERS_TOUCH_MS && local.amount > 0) out.set(k, local)
    }
    return sortHolders([...out.values()]).slice(0, 20)
  }

  return {
    market_id: api.market_id ?? prev?.market_id ?? null,
    condition_id: api.condition_id ?? prev?.condition_id ?? null,
    updated_at: Date.now(),
    live: true,
    up: mergeSide('up', api.up ?? [], prev?.up ?? []),
    down: mergeSide('down', api.down ?? [], prev?.down ?? []),
  }
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
  const [liveActivityTrades, setLiveActivityTrades] = useState<LiveActivityTrade[]>([])
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
  const wsRef = useRef<WebSocket | null>(null)
  const liveWsRef = useRef<WebSocket | null>(null)
  const liveActiveRef = useRef(liveActive)
  const liveMarketIdRef = useRef('')
  const liveReconnectTimer = useRef<number | null>(null)
  const liveVolumeRefreshTimer = useRef<number | null>(null)
  const holdersTouchedRef = useRef<Map<string, number>>(new Map())
  const liveReconnectAttempt = useRef(0)
  const effectiveSplitRef = useRef(effectiveSplit)
  const selectedDateRef = useRef(selectedDate)
  const selectedTimeRef = useRef(selectedTime)
  const catalogRefreshTimer = useRef<number | null>(null)
  liveActiveRef.current = liveActive
  effectiveSplitRef.current = effectiveSplit
  selectedDateRef.current = selectedDate
  selectedTimeRef.current = selectedTime

  /** Drop the in-progress live window from history lists. */
  const filterHistoryMarkets = (list: MarketSummary[], now = Date.now()) =>
    list.filter((m) => {
      const start = Number(m.start_time) || 0
      const end = Number(m.end_time) || 0
      if (end <= 0 || end > now) return false
      if (start <= now && now < end) return false
      return true
    })

  const neighborsFromMarkets = (list: MarketSummary[], mid: string) => {
    const chron = [...list].sort((a, b) => (a.start_time || 0) - (b.start_time || 0))
    const i = chron.findIndex((m) => m.market_id === mid)
    if (i < 0) return { prev: null as string | null, next: null as string | null }
    return {
      prev: i > 0 ? chron[i - 1].market_id : null,
      next: i + 1 < chron.length ? chron[i + 1].market_id : null,
    }
  }

  const applyMarketsList = (list: MarketSummary[]) => {
    const ordered = filterHistoryMarkets(list).sort(
      (a, b) => (b.start_time || 0) - (a.start_time || 0),
    )
    if (!ordered.length) {
      setMarkets([])
      setSelectedTime('')
      setMarketId('')
      setNeighbors({ prev: null, next: null })
      return
    }
    setMarkets(ordered)
    setMarketId((prev) => {
      const keep = ordered.find((m) => m.market_id === prev)
      if (keep) {
        setSelectedTime(keep.time_et || '')
        setNeighbors(neighborsFromMarkets(ordered, keep.market_id))
        return prev
      }
      const still = ordered.find((m) => (m.time_et || '') === selectedTimeRef.current)
      const pick = still || ordered[0]
      setSelectedTime(pick.time_et || '')
      setNeighbors(neighborsFromMarkets(ordered, pick.market_id))
      return pick.market_id
    })
  }

  /** Refresh calendar + day slots (e.g. after each 5m live market ends). */
  const refreshMarketCatalog = async (opts?: { rebuild?: boolean; preferLatest?: boolean }) => {
    const split = effectiveSplitRef.current
    const rebuild = Boolean(opts?.rebuild)
    try {
      const datesRes = await api.marketDates(split, { rebuild_index: rebuild })
      setDateMin(datesRes.min || '')
      setDateMax(datesRes.max || '')
      let date = selectedDateRef.current
      const onLatest = !date || date === datesRes.max || !datesRes.dates.includes(date)
      if (opts?.preferLatest || onLatest) {
        date = datesRes.max || datesRes.dates[datesRes.dates.length - 1] || ''
        if (date) setSelectedDate(date)
      }
      if (!date) return
      const res = await api.markets(split, { date, rebuild_index: rebuild })
      // Only apply into UI when not live (list is for history sidebar); still warm cache when live.
      if (!liveActiveRef.current) {
        applyMarketsList(res.markets)
      } else {
        // Keep in-memory list warm for when user exits live (still hide current window).
        const ordered = filterHistoryMarkets(res.markets).sort(
          (a, b) => (b.start_time || 0) - (a.start_time || 0),
        )
        setMarkets(ordered)
      }
    } catch {
      // Non-fatal — live/history UI continues.
    }
  }

  const clearLiveReconnectTimer = () => {
    if (liveReconnectTimer.current != null) {
      window.clearTimeout(liveReconnectTimer.current)
      liveReconnectTimer.current = null
    }
  }

  const clearLiveVolumeRefresh = () => {
    if (liveVolumeRefreshTimer.current != null) {
      window.clearInterval(liveVolumeRefreshTimer.current)
      liveVolumeRefreshTimer.current = null
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
        applyMarketsList(res.markets)
      })
      .catch((e) => {
        if (!cancelled) setError(String(e))
      })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [effectiveSplit, selectedDate, liveActive])

  // Refresh TWAP/history catalog ~2s after each 5m wall-clock boundary (market finish).
  useEffect(() => {
    const PERIOD_MS = 300_000
    const delayToNext = () => {
      const now = Date.now()
      return Math.max(500, Math.ceil(now / PERIOD_MS) * PERIOD_MS + 2_000 - now)
    }
    const arm = () => {
      if (catalogRefreshTimer.current != null) window.clearTimeout(catalogRefreshTimer.current)
      catalogRefreshTimer.current = window.setTimeout(async () => {
        catalogRefreshTimer.current = null
        await refreshMarketCatalog({
          rebuild: true,
          preferLatest: liveActiveRef.current || effectiveSplitRef.current === 'twap',
        })
        arm()
      }, delayToNext())
    }
    arm()
    return () => {
      if (catalogRefreshTimer.current != null) {
        window.clearTimeout(catalogRefreshTimer.current)
        catalogRefreshTimer.current = null
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [effectiveSplit])

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

  // Keep Prev/Next aligned with the filtered history list (no jump into live).
  useEffect(() => {
    if (liveActive || !marketId || !markets.length) return
    setNeighbors(neighborsFromMarkets(markets, marketId))
  }, [liveActive, marketId, markets])

  // History: load top holders + activity tape for the selected market.
  useEffect(() => {
    if (liveActive || !marketId) return
    let cancelled = false
    setHolders(null)
    setLiveActivityTrades([])
    holdersTouchedRef.current.clear()
    Promise.all([api.marketHolders(marketId, 20), api.marketActivity(marketId, 1500)])
      .then(([h, a]) => {
        if (cancelled) return
        setHolders({
          market_id: h.market_id ?? marketId,
          condition_id: h.condition_id ?? null,
          updated_at: h.updated_at ?? Date.now(),
          live: false,
          up: sortHolders(h.up),
          down: sortHolders(h.down),
        })
        setHoldersRevision((n) => n + 1)
        // Keep full window tape for playhead scrubbing (Data API newest-first pages).
        setLiveActivityTrades(
          [...(a.trades ?? [])].sort((x, y) => y.timestamp - x.timestamp),
        )
      })
      .catch(() => {
        if (cancelled) return
        setHolders({
          market_id: marketId,
          condition_id: null,
          updated_at: Date.now(),
          live: false,
          up: [],
          down: [],
        })
        setLiveActivityTrades([])
      })
    return () => {
      cancelled = true
    }
  }, [liveActive, marketId])

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
            ...volumeFields(p),
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
          ...volumeFields(p),
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
    // Keep detail.series (with volumes) + social tape; chart/feed scrub to playhead.
    setSeriesLive([])
    setTick(null)
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
    clearLiveVolumeRefresh()
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
      setLiveActivityTrades([])
      setBook(null)
      setHolders(null)
      setHoldersRevision(0)
      setLiveMarketId('')
      liveMarketIdRef.current = ''
      setLiveWindow(null)
      liveReconnectAttempt.current = 0
    }
    setError(null)

    const interval = Math.max(0.1, Math.min(2, liveInterval))
    const ws = new WebSocket(wsUrl('/api/ws/live'))
    liveWsRef.current = ws

    const applySeriesSeed = (
      res: {
        market_id?: string | null
        series?: LiveSeriesPoint[]
      },
      reqId?: string,
    ) => {
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
          ...volumeFields(p),
        }))
      if (!points.length) return
      setSeriesLive((prev) => {
        // Seed carries parquet volume; keep larger of seed vs live RTDS bumps.
        const byT = new Map<number, LiveSeriesPoint>()
        for (const p of points) byT.set(p.t, p)
        for (const p of prev) {
          const seeded = byT.get(p.t)
          if (seeded) {
            byT.set(p.t, {
              ...seeded,
              up: p.up ?? seeded.up,
              down: p.down ?? seeded.down,
              btc: p.btc ?? seeded.btc,
              twap: p.twap ?? seeded.twap,
              chainlink: p.chainlink ?? seeded.chainlink,
              ...maxVolumeFields(volumeFields(seeded), volumeFields(p)),
            })
          } else {
            byT.set(p.t, { ...p, ...volumeFields(p) })
          }
        }
        const merged = [...byT.values()].sort((a, b) => a.t - b.t)
        return merged.length > 1200 ? merged.slice(-1200) : merged
      })
    }

    ws.onopen = () => {
      if (liveWsRef.current !== ws) return
      liveReconnectAttempt.current = 0
      setError(null)
      ws.send(JSON.stringify({ interval_s: interval }))
      // Chart backfill arrives via WS `series` messages (no HTTP poll).
      clearLiveVolumeRefresh()
    }
    ws.onmessage = (ev) => {
      if (liveWsRef.current !== ws) return
      const msg = JSON.parse(ev.data) as Tick
      if (msg.type === 'error') {
        setError(msg.message || 'Live feed error')
        return
      }
      if (msg.type === 'series') {
        const res = msg as { market_id?: string | null; series?: LiveSeriesPoint[] }
        applySeriesSeed(res, liveMarketIdRef.current || undefined)
        return
      }
      if (msg.type === 'market') {
        if (msg.market_id) {
          const mid = String(msg.market_id)
          liveMarketIdRef.current = mid
          setLiveMarketId(mid)
        }
        if (msg.start_time != null && msg.end_time != null) {
          setLiveWindow({ start: Number(msg.start_time), end: Number(msg.end_time) })
        }
        // Clear prior window quotes so Price To Beat doesn't stick across markets.
        setTick(null)
        setSeriesLive([])
        setLiveActivityTrades([])
        setBook(null)
        setHolders(null)
        holdersTouchedRef.current.clear()
        // Closed market just rolled — rebuild TWAP catalog after VPS sync lands.
        window.setTimeout(() => {
          void refreshMarketCatalog({ rebuild: true, preferLatest: true })
        }, 3_000)
        return
      }
      if (msg.type === 'holders') {
        const api: LiveHoldersResponse = {
          market_id: msg.market_id ?? null,
          condition_id: msg.condition_id ?? null,
          updated_at: msg.updated_at ?? Date.now(),
          live: true,
          up: sortHolders(msg.up),
          down: sortHolders(msg.down),
        }
        setHolders((prev) => mergeHoldersFromApi(api, prev, holdersTouchedRef.current))
        setHoldersRevision((n) => n + 1)
        return
      }
      if (msg.type === 'activity') {
        const incoming = (msg as { trades?: LiveActivityTrade[] }).trades ?? []
        if (!incoming.length) return
        setLiveActivityTrades((prev) => {
          const byId = new Map<string, LiveActivityTrade>()
          for (const t of incoming) {
            if (t?.id) byId.set(String(t.id), t)
          }
          for (const t of prev) byId.set(t.id, t)
          return [...byId.values()]
            .sort((a, b) => b.timestamp - a.timestamp)
            .slice(0, 80)
        })
        setSeriesLive((prev) => {
          let next = prev
          for (const t of incoming) {
            next = applyActivityTradeToSeries(next, t)
          }
          return next
        })
        // Holders Data API lags; mirror fills into the board immediately.
        setHolders((prev) => {
          let next = prev
          for (const t of incoming) {
            next = applyActivityTradeToHolders(next, t, holdersTouchedRef.current)
          }
          return next
        })
        setHoldersRevision((n) => n + 1)
        return
      }
      if (msg.type === 'tick') {
        // Soft feed notes (e.g. no active market) — don't sticky-banner WS errors.
        if (msg.error) setError(String(msg.error))
        else setError(null)
        setTick(msg)
        if (msg.market_id) {
          const mid = String(msg.market_id)
          liveMarketIdRef.current = mid
          setLiveMarketId(mid)
        }
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
              ...volumeFields({}),
            }
            if (!prev.length) return [point]
            const last = prev[prev.length - 1]
            if (last.t === point.t) {
              const next = prev.slice(0, -1)
              // Keep volume from the seeded point; tick only updates prices.
              next.push({ ...last, ...point, ...volumeFields(last) })
              return next
            }
            if (point.t < last.t) {
              // Rare clock skew / late seed overlap — ignore older live tick.
              return prev
            }
            const next = [...prev, point]
            return next.length > 1200 ? next.slice(-1200) : next
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
      clearLiveVolumeRefresh()
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
      clearLiveVolumeRefresh()
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
      clearLiveVolumeRefresh()
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
      liveMarketIdRef.current = ''
      setLiveWindow(null)
      setHolders(null)
      setLiveActivityTrades([])
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
            ...volumeFields(p),
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

  // Wall-clock tick: live charts (100ms) + history activity "ago" labels (1s).
  useEffect(() => {
    const id = window.setInterval(() => setNowMs(Date.now()), liveActive ? 100 : 1000)
    return () => window.clearInterval(id)
  }, [liveActive])

  // History replay: scrub charts/feed to the current tick (like live).
  const playheadTs = useMemo(() => {
    if (liveActive) return null
    if (!(playing || paused)) return null
    if (tick?.timestamp != null && Number.isFinite(tick.timestamp)) return tick.timestamp
    if (detail?.start_time != null) return detail.start_time
    return null
  }, [liveActive, playing, paused, tick?.timestamp, detail?.start_time])

  const historySeriesFull = useMemo(() => {
    return (detail?.series ?? []).map((p) => ({
      t: p.t,
      up: p.up ?? 0,
      down: p.down ?? 0,
      btc: p.btc,
      twap: p.twap ?? null,
      chainlink: p.chainlink ?? null,
      ...volumeFields(p),
    }))
  }, [detail])

  const chartData = useMemo(() => {
    if (liveActive) return seriesLive
    if (playheadTs != null) {
      if (historySeriesFull.length) {
        return historySeriesFull.filter((p) => p.t <= playheadTs)
      }
      return seriesLive.filter((p) => p.t <= playheadTs)
    }
    if (historySeriesFull.length) return historySeriesFull
    return seriesLive
  }, [liveActive, seriesLive, historySeriesFull, playheadTs])

  const displayActivityTrades = useMemo(() => {
    if (liveActive || playheadTs == null) return liveActivityTrades
    return liveActivityTrades.filter((t) => Number(t.timestamp) <= playheadTs)
  }, [liveActive, playheadTs, liveActivityTrades])

  const displayHolders = useMemo(() => {
    if (liveActive || playheadTs == null) return holders
    return holdersFromActivity(liveActivityTrades, playheadTs, {
      market_id: marketId || holders?.market_id || null,
      condition_id: holders?.condition_id ?? null,
    })
  }, [liveActive, playheadTs, holders, liveActivityTrades, marketId])

  // Activity "Xs ago" uses market clock: playhead while replaying, else window end.
  const feedNowMs = liveActive
    ? nowMs
    : (playheadTs ?? detail?.end_time ?? detail?.start_time ?? nowMs)
  const feedLive = liveActive || playing || paused

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

  // Live / history play: trailing window. Historical idle: full 5m market.
  const xDefaultDomain = useMemo((): TimeDomain => {
    const [f0, f1] = xFullDomain
    if (!liveActive && !playing && !paused) {
      const end = Number.isFinite(f1) && f1 > f0 ? f1 : f0 + 300_000
      const start = Number.isFinite(f0) ? f0 : end - 300_000
      return [start, end]
    }
    const latestData =
      playheadTs != null
        ? playheadTs
        : chartData.length > 0
          ? chartData[chartData.length - 1].t
          : f0
    const end = liveActive
      ? Math.min(f1, Math.max(latestData, nowMs))
      : Math.min(f1, Math.max(f0 + 1, latestData))
    const start = end - DEFAULT_X_SPAN_MS
    return [start, end]
  }, [xFullDomain, chartData, liveActive, nowMs, playing, paused, playheadTs])

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

  /** Outcome from meta.json only (winner / closed) — no price inference. */
  const historyOutcome = useMemo((): 'Up' | 'Down' | 'not_closed' | null => {
    if (liveActive || playing) return null
    if (!detail) return 'not_closed'
    if (detail.closed === false) return 'not_closed'
    const w = detail.winner
    if (w === 1) return 'Up'
    if (w === 0) return 'Down'
    return 'not_closed'
  }, [liveActive, playing, detail])

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
      <aside className="workspace-rail workspace-rail-left">
        {liveActive ? (
          <>
            <div className="live-exit-bar">
              <span className="live-exit-label">{liveLabel || 'LIVE'}</span>
              <button type="button" className="sidebar-btn" onClick={toggleLive}>
                Exit live
              </button>
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
            />
          </>
        ) : (
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
        )}
      </aside>

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
          outcome={!liveActive ? historyOutcome : null}
          resolvedAt={!liveActive ? detail?.resolved_at ?? null : null}
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
          <VolumeChart
            data={chartData}
            mode="binance"
            title="Binance BTC volume"
            xDomain={sharedXDomain}
            hoverTime={sharedHoverTime}
            onHoverTimeChange={setSharedHoverTime}
            live={liveActive}
            nowMs={liveActive ? nowMs : undefined}
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
          <VolumeChart
            data={chartData}
            mode="outcomes"
            title="Up / Down volume"
            xDomain={sharedXDomain}
            hoverTime={sharedHoverTime}
            onHoverTimeChange={setSharedHoverTime}
            live={liveActive}
            nowMs={liveActive ? nowMs : undefined}
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

      <FeedSidebar
        enabled={liveActive || Boolean(marketId)}
        live={feedLive}
        holders={displayHolders}
        holdersRevision={holdersRevision}
        activityTrades={displayActivityTrades}
        nowMs={feedNowMs}
      />
    </div>
  )
}
