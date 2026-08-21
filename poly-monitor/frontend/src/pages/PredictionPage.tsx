import { useEffect, useMemo, useRef, useState } from 'react'
import {
  api,
  formatCentsTrade,
  formatPct,
  wsUrl,
  type DirectionPrediction,
  type DirectionModelsResponse,
  type LiveDirectionPrediction,
  type LiveSeriesPoint,
} from '../api'
import PredictionDistChart from '../components/PredictionDistChart'
import PriceChart, { type TimeDomain } from '../components/PriceChart'
import {
  SERIES_WINDOW_MS,
  type MarketSeriesKey,
  loadMarketSeries,
  saveMarketSeries,
  seriesLabel,
} from '../series'

const MAX_SERIES_POINTS = 1200
const WS_RECONNECT_MS = 1_500

function ageLabel(ageMs: number) {
  if (!Number.isFinite(ageMs) || ageMs < 0) return '—'
  if (ageMs < 1_000) return 'just now'
  return `${(ageMs / 1000).toFixed(ageMs < 10_000 ? 1 : 0)}s ago`
}

function lastFinite(points: LiveSeriesPoint[], key: 'up' | 'down'): number | null {
  for (let i = points.length - 1; i >= 0; i--) {
    const v = points[i][key]
    if (v != null && Number.isFinite(v)) return Number(v)
  }
  return null
}

function num(value: unknown): number | null {
  const n = Number(value)
  return value != null && Number.isFinite(n) ? n : null
}

function mapSeriesPoints(raw: LiveSeriesPoint[] | undefined): LiveSeriesPoint[] {
  return (raw ?? [])
    .filter((p) => p.t != null && Number.isFinite(Number(p.t)))
    .map((p) => ({
      t: Number(p.t),
      up: p.up ?? null,
      down: p.down ?? null,
      btc: p.btc ?? null,
      twap: p.twap ?? null,
      chainlink: p.chainlink ?? null,
    }))
}

function mergeSeriesSeed(prev: LiveSeriesPoint[], seeded: LiveSeriesPoint[]): LiveSeriesPoint[] {
  if (!seeded.length) return prev
  const byT = new Map<number, LiveSeriesPoint>()
  for (const p of seeded) byT.set(p.t, p)
  for (const p of prev) {
    const existing = byT.get(p.t)
    if (existing) {
      byT.set(p.t, {
        ...existing,
        up: p.up ?? existing.up,
        down: p.down ?? existing.down,
        btc: p.btc ?? existing.btc,
        twap: p.twap ?? existing.twap,
        chainlink: p.chainlink ?? existing.chainlink,
      })
    } else {
      byT.set(p.t, p)
    }
  }
  const merged = [...byT.values()].sort((a, b) => a.t - b.t)
  return merged.length > MAX_SERIES_POINTS ? merged.slice(-MAX_SERIES_POINTS) : merged
}

function appendTickPoint(
  prev: LiveSeriesPoint[],
  msg: {
    timestamp?: number
    up_price?: number | null
    down_price?: number | null
    btc_price?: number | null
    btc_twap_30s?: number | null
    btc_chainlink?: number | null
  },
): LiveSeriesPoint[] {
  const t = Math.floor(Number(msg.timestamp) / 1000) * 1000
  if (!Number.isFinite(t)) return prev
  const point: LiveSeriesPoint = {
    t,
    up: msg.up_price ?? null,
    down: msg.down_price ?? null,
    btc: msg.btc_price ?? null,
    twap: msg.btc_twap_30s ?? null,
    chainlink: msg.btc_chainlink ?? null,
  }
  const mergePrice = (
    next: number | null | undefined,
    prevVal: number | null | undefined,
  ): number | null =>
    next != null && Number.isFinite(next) ? next : prevVal != null && Number.isFinite(prevVal) ? prevVal : null
  const mergeInto = (base: LiveSeriesPoint, next: LiveSeriesPoint): LiveSeriesPoint => ({
    ...base,
    ...next,
    up: mergePrice(next.up, base.up),
    down: mergePrice(next.down, base.down),
    btc: mergePrice(next.btc, base.btc),
    twap: mergePrice(next.twap, base.twap),
    chainlink: mergePrice(next.chainlink, base.chainlink),
  })
  if (!prev.length) return [point]
  const last = prev[prev.length - 1]
  if (last.t === point.t) {
    return [...prev.slice(0, -1), mergeInto(last, point)]
  }
  if (point.t > last.t) {
    const next = [...prev, point]
    return next.length > MAX_SERIES_POINTS ? next.slice(-MAX_SERIES_POINTS) : next
  }
  // Out-of-order / delayed tick — keep series strictly time-sorted.
  let lo = 0
  let hi = prev.length
  while (lo < hi) {
    const mid = (lo + hi) >> 1
    if (prev[mid].t < point.t) lo = mid + 1
    else hi = mid
  }
  if (lo < prev.length && prev[lo].t === point.t) {
    const copy = prev.slice()
    copy[lo] = mergeInto(prev[lo], point)
    return copy
  }
  const copy = prev.slice()
  copy.splice(lo, 0, point)
  return copy.length > MAX_SERIES_POINTS ? copy.slice(-MAX_SERIES_POINTS) : copy
}

function PredictionChip({
  prediction,
  kind,
}: {
  prediction: DirectionPrediction
  kind: 'direction' | 'beta'
}) {
  const isUp = prediction.direction === 'UP'
  const probability = isUp ? prediction.probability_up : prediction.probability_down
  return (
    <article className={`prediction-chip ${isUp ? 'prediction-up' : 'prediction-down'}`}>
      <div className="prediction-chip-header">
        <span className="prediction-chip-horizon">{prediction.horizon_seconds}s horizon</span>
        <span className={`prediction-chip-dir ${isUp ? 'up' : 'down'}`}>{prediction.direction}</span>
      </div>
      <div className="prediction-chip-value">
        {kind === 'beta' && prediction.mean != null
          ? formatCentsTrade(prediction.mean)
          : formatPct(probability)}
      </div>
      <div
        className="prediction-bar"
        aria-label={`Up ${formatPct(prediction.probability_up)}, Down ${formatPct(prediction.probability_down)}`}
      >
        <span className="prediction-bar-up" style={{ width: `${prediction.probability_up * 100}%` }} />
        <span className="prediction-bar-down" style={{ width: `${prediction.probability_down * 100}%` }} />
      </div>
      <div className="prediction-chip-foot">
        {kind === 'beta' ? (
          <>
            <span>P(rise)</span>
            <strong>{formatPct(prediction.probability_up)}</strong>
          </>
        ) : (
          <>
            <span>Confidence</span>
            <strong>{formatPct(prediction.confidence)}</strong>
          </>
        )}
      </div>
    </article>
  )
}

export default function PredictionPage() {
  const [result, setResult] = useState<LiveDirectionPrediction | null>(null)
  const [series, setSeries] = useState<LiveSeriesPoint[]>([])
  const [error, setError] = useState<string | null>(null)
  const [updatedAt, setUpdatedAt] = useState<number | null>(null)
  const [chartXDomain, setChartXDomain] = useState<TimeDomain | null>(null)
  const [followLiveX, setFollowLiveX] = useState(true)
  const [hoverTime, setHoverTime] = useState<number | null>(null)
  const [models, setModels] = useState<DirectionModelsResponse | null>(null)
  const [selectedHorizons, setSelectedHorizons] = useState<number[]>([])
  const [customHorizons, setCustomHorizons] = useState('3,5')
  const [modelAction, setModelAction] = useState<'train' | 'evaluate'>('evaluate')
  const [modelKind, setModelKind] = useState<'direction' | 'beta'>('direction')
  const [trainKind, setTrainKind] = useState<'direction' | 'beta' | 'beta_ct'>('beta_ct')
  const [modelHorizon, setModelHorizon] = useState('3')
  const [modelBusy, setModelBusy] = useState(false)
  const [modelError, setModelError] = useState<string | null>(null)
  const [liveKind, setLiveKind] = useState<'direction' | 'beta'>('direction')
  const [continuousReady, setContinuousReady] = useState(false)
  const [marketSeries, setMarketSeriesState] = useState<MarketSeriesKey>(() => loadMarketSeries())
  const marketSeriesRef = useRef(marketSeries)
  marketSeriesRef.current = marketSeries
  const MARKET_WINDOW_MS = SERIES_WINDOW_MS[marketSeries]
  const setMarketSeries = (s: MarketSeriesKey) => {
    if (s === marketSeriesRef.current) return
    saveMarketSeries(s)
    setMarketSeriesState(s)
  }
  const marketIdRef = useRef<string | null>(null)
  const marketStartRef = useRef<number | null>(null)
  const wsRef = useRef<WebSocket | null>(null)
  const kindBootstrapped = useRef(false)

  const parseHorizonList = (raw: string): number[] => {
    const values = raw
      .split(/[,\s]+/)
      .map((part) => Number(part.trim()))
      .filter((n) => Number.isFinite(n) && n > 0 && n <= 60)
    return [...new Set(values.map((n) => Number(n.toFixed(3))))].sort((a, b) => a - b)
  }

  useEffect(() => {
    let cancelled = false
    const load = async () => {
      try {
        const next = await api.directionModels(modelKind)
        if (cancelled) return
        if (!kindBootstrapped.current) {
          kindBootstrapped.current = true
          const live = next.active_kind ?? 'direction'
          setLiveKind(live)
          if (live !== modelKind) {
            setModelKind(live)
            return
          }
        }
        setModels(next)
        setContinuousReady(Boolean(next.continuous_t_ready))
        setLiveKind(next.active_kind ?? 'direction')
        setSelectedHorizons((current) => {
          if (next.active_kind === modelKind && next.active_horizons.length) {
            setCustomHorizons(next.active_horizons.join(','))
            return next.active_horizons
          }
          if (current.length && current.every((h) => next.models.some((m) => m.horizon_seconds === h))) {
            return current
          }
          const fallback = next.models.map((m) => m.horizon_seconds)
          if (fallback.length) setCustomHorizons(fallback.join(','))
          return fallback
        })
        setModelError(null)
      } catch (err) {
        if (!cancelled) setModelError(err instanceof Error ? err.message : String(err))
      }
    }
    void load()
    const id = window.setInterval(() => {
      if (models?.job?.status === 'running') void load()
    }, 2_000)
    return () => {
      cancelled = true
      window.clearInterval(id)
    }
  }, [models?.job?.status, modelKind])

  useEffect(() => {
    let cancelled = false
    let reconnectTimer: number | null = null

    marketIdRef.current = null
    marketStartRef.current = null
    setSeries([])
    setResult(null)
    setHoverTime(null)
    setChartXDomain(null)
    setFollowLiveX(true)

    const clearReconnect = () => {
      if (reconnectTimer != null) {
        window.clearTimeout(reconnectTimer)
        reconnectTimer = null
      }
    }

    const resetChartState = () => {
      setSeries([])
      setResult(null)
      setHoverTime(null)
      setChartXDomain(null)
      setFollowLiveX(true)
    }

    /** Adopt a market id; returns true when rolling off a prior market. */
    const beginMarket = (mid: string | null | undefined, startMs?: number | null): boolean => {
      if (!mid) return false
      const next = String(mid)
      if (startMs != null && Number.isFinite(startMs)) marketStartRef.current = Number(startMs)
      if (marketIdRef.current === next) return false
      const rolled = marketIdRef.current != null
      marketIdRef.current = next
      if (rolled) resetChartState()
      return rolled
    }

    const applyPrediction = (prediction: LiveDirectionPrediction) => {
      // Ignore predictions for a market we already left.
      if (
        marketIdRef.current &&
        prediction.market_id &&
        prediction.market_id !== marketIdRef.current
      ) {
        return
      }
      beginMarket(prediction.market_id)
      setResult(prediction)
      setUpdatedAt(Date.now())
      setError(null)
    }

    const connect = () => {
      if (cancelled) return
      clearReconnect()
      const ws = new WebSocket(wsUrl('/api/ws/live'))
      wsRef.current = ws

      ws.onopen = () => {
        if (cancelled || wsRef.current !== ws) return
        setError(null)
        ws.send(
          JSON.stringify({
            interval_s: 0.5,
            want_direction: true,
            series: marketSeriesRef.current,
          }),
        )
      }

      ws.onmessage = (ev) => {
        if (cancelled || wsRef.current !== ws) return
        let msg: Record<string, unknown>
        try {
          msg = JSON.parse(String(ev.data)) as Record<string, unknown>
        } catch {
          return
        }
        const type = String(msg.type || '')

        if (type === 'error') {
          setError(String(msg.message || 'Live feed error'))
          return
        }
        if (type === 'direction_prediction_error') {
          setError(String(msg.message || 'Prediction unavailable'))
          return
        }
        if (type === 'direction_prediction') {
          const prediction = msg as unknown as LiveDirectionPrediction
          if (prediction.model_kind === 'beta' || prediction.model_kind === 'direction') {
            setLiveKind(prediction.model_kind)
          }
          applyPrediction(prediction)
          return
        }
        if (type === 'series') {
          const mid = msg.market_id != null ? String(msg.market_id) : null
          // Drop seeds for a market we already rolled past (don't roll back).
          if (mid && marketIdRef.current && mid !== marketIdRef.current) return
          if (mid && !marketIdRef.current) marketIdRef.current = mid
          const points = mapSeriesPoints(msg.series as LiveSeriesPoint[] | undefined)
          const start = marketStartRef.current
          const clipped =
            start != null ? points.filter((p) => p.t >= start - 1_000) : points
          if (!clipped.length) return
          setSeries((prev) => mergeSeriesSeed(prev, clipped))
          return
        }
        if (type === 'market') {
          const mid = msg.market_id != null ? String(msg.market_id) : null
          const start = num(msg.start_time)
          if (start != null) marketStartRef.current = start
          if (mid) marketIdRef.current = mid
          // Always clear on market announcements (matches MarketPage).
          resetChartState()
          return
        }
        if (type === 'tick') {
          if (msg.error) setError(String(msg.error))
          else setError(null)
          const mid = msg.market_id != null ? String(msg.market_id) : null
          const rolled = beginMarket(mid, num(msg.start_time))
          setSeries((prev) =>
            appendTickPoint(rolled ? [] : prev, {
              timestamp: num(msg.timestamp) ?? undefined,
              up_price: num(msg.up_price),
              down_price: num(msg.down_price),
              btc_price: num(msg.btc_price),
              btc_twap_30s: num(msg.btc_twap_30s),
              btc_chainlink: num(msg.btc_chainlink),
            }),
          )
          setUpdatedAt(Date.now())
        }
      }

      ws.onclose = () => {
        if (cancelled || wsRef.current !== ws) return
        wsRef.current = null
        setError('Live WebSocket disconnected — reconnecting…')
        reconnectTimer = window.setTimeout(connect, WS_RECONNECT_MS)
      }

      ws.onerror = () => {
        // onclose handles reconnect; avoid duplicate banners.
      }
    }

    connect()
    return () => {
      cancelled = true
      clearReconnect()
      const ws = wsRef.current
      wsRef.current = null
      if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
        ws.close()
      }
    }
  }, [marketSeries])

  const chartData = useMemo(() => {
    const history = result?.history ?? []
    const preds = result?.predictions ?? []
    const primaryH = preds[0]?.horizon_seconds ?? 3
    const meanKey = `mean_${String(primaryH).replace('.', 'p')}s`
    const stdKey = `std_${String(primaryH).replace('.', 'p')}s`
    const pKey = `p_up_${String(primaryH).replace('.', 'p')}s`
    const windowStart = marketStartRef.current

    const histMeans: { t: number; mean: number; std: number | null }[] = []
    for (const h of history) {
      const t = Number(h.timestamp)
      if (!Number.isFinite(t)) continue
      if (windowStart != null && t < windowStart - 1_000) continue
      const meanRaw = h[meanKey]
      const mean =
        typeof meanRaw === 'number' && Number.isFinite(meanRaw)
          ? meanRaw
          : typeof h[pKey] === 'number' && Number.isFinite(h[pKey] as number)
            ? (h[pKey] as number) // direction: show P(up) on same axis as a score
            : null
      if (mean == null) continue
      const stdRaw = h[stdKey]
      histMeans.push({
        t,
        mean,
        std: typeof stdRaw === 'number' && Number.isFinite(stdRaw) ? stdRaw : null,
      })
    }

    const nearestPred = (t: number): { mean: number; std: number | null } | null => {
      if (!histMeans.length) return null
      let best = histMeans[0]
      let bestDist = Math.abs(best.t - t)
      for (let i = 1; i < histMeans.length; i++) {
        const d = Math.abs(histMeans[i].t - t)
        if (d < bestDist) {
          best = histMeans[i]
          bestDist = d
        }
      }
      return bestDist <= 2_500 ? best : null
    }

    const liveSeries = (
      windowStart != null ? series.filter((p) => p.t >= windowStart - 1_000) : series
    )
      .slice()
      .sort((a, b) => a.t - b.t)

    type ChartRow = {
      t: number
      up: number | null
      down: number | null
      btc: number | null
      twap: number | null
      chainlink: number | null
      upPred: number | null
      upPredLo: number | null
      upPredHi: number | null
    }

    const byT = new Map<number, ChartRow>()
    const upsert = (row: ChartRow) => {
      const prev = byT.get(row.t)
      if (!prev) {
        byT.set(row.t, row)
        return
      }
      byT.set(row.t, {
        t: row.t,
        up: row.up ?? prev.up,
        down: row.down ?? prev.down,
        btc: row.btc ?? prev.btc,
        twap: row.twap ?? prev.twap,
        chainlink: row.chainlink ?? prev.chainlink,
        upPred: row.upPred ?? prev.upPred,
        upPredLo: row.upPredLo ?? prev.upPredLo,
        upPredHi: row.upPredHi ?? prev.upPredHi,
      })
    }

    for (const p of liveSeries) {
      const hit = nearestPred(p.t)
      const mean = hit?.mean ?? null
      const std = hit?.std
      upsert({
        t: p.t,
        up: p.up ?? null,
        down: p.down ?? null,
        btc: p.btc ?? null,
        twap: p.twap ?? null,
        chainlink: p.chainlink ?? null,
        upPred: mean,
        upPredLo: mean != null && std != null ? Math.max(0, mean - std) : null,
        upPredHi: mean != null && std != null ? Math.min(1, mean + std) : null,
      })
    }

    // Forward forecast path from the live tip through 5s (1s steps).
    const FORECAST_HORIZON_S = 5
    if (liveSeries.length && preds.length) {
      const last = liveSeries[liveSeries.length - 1]
      const lastT = last.t
      const lastUp = last.up
      const tipMean =
        lastUp != null && Number.isFinite(lastUp)
          ? lastUp
          : preds.find((p) => p.mean != null)?.mean ?? null

      const knots: { s: number; mean: number; std: number | null }[] = []
      if (tipMean != null && Number.isFinite(tipMean)) {
        knots.push({ s: 0, mean: tipMean, std: 0 })
      }
      for (const pred of [...preds].sort((a, b) => a.horizon_seconds - b.horizon_seconds)) {
        let mean = pred.mean ?? null
        if (mean == null && lastUp != null && Number.isFinite(lastUp)) {
          const edge = (pred.probability_up - 0.5) * 2
          mean = Math.min(1, Math.max(0, lastUp + edge * 0.03))
        }
        if (mean == null || !Number.isFinite(mean)) continue
        knots.push({
          s: Math.max(0, pred.horizon_seconds),
          mean,
          std: pred.std != null && Number.isFinite(pred.std) ? pred.std : null,
        })
      }
      // Guarantee a knot at 5s so the path always reaches the requested horizon.
      if (knots.length) {
        const lastKnot = knots[knots.length - 1]
        if (lastKnot.s < FORECAST_HORIZON_S) {
          knots.push({ s: FORECAST_HORIZON_S, mean: lastKnot.mean, std: lastKnot.std })
        } else if (!knots.some((k) => Math.abs(k.s - FORECAST_HORIZON_S) < 1e-6)) {
          let lo = knots[0]
          let hi = knots[knots.length - 1]
          for (const k of knots) {
            if (k.s <= FORECAST_HORIZON_S) lo = k
            if (k.s >= FORECAST_HORIZON_S) {
              hi = k
              break
            }
          }
          const w = hi.s === lo.s ? 0 : (FORECAST_HORIZON_S - lo.s) / (hi.s - lo.s)
          const mean = lo.mean + (hi.mean - lo.mean) * w
          const std =
            lo.std != null && hi.std != null ? lo.std + (hi.std - lo.std) * w : (hi.std ?? lo.std)
          knots.push({ s: FORECAST_HORIZON_S, mean, std })
          knots.sort((a, b) => a.s - b.s)
        }
      }

      const sampleAt = (s: number): { mean: number; std: number | null } | null => {
        if (!knots.length) return null
        if (s <= knots[0].s) return { mean: knots[0].mean, std: knots[0].std }
        for (let i = 1; i < knots.length; i++) {
          const a = knots[i - 1]
          const b = knots[i]
          if (s <= b.s) {
            const w = b.s === a.s ? 0 : (s - a.s) / (b.s - a.s)
            return {
              mean: a.mean + (b.mean - a.mean) * w,
              std:
                a.std != null && b.std != null ? a.std + (b.std - a.std) * w : (b.std ?? a.std),
            }
          }
        }
        const tip = knots[knots.length - 1]
        return { mean: tip.mean, std: tip.std }
      }

      const tipHit = sampleAt(0)
      if (tipHit) {
        upsert({
          t: lastT,
          up: lastUp ?? null,
          down: last.down ?? null,
          btc: null,
          twap: null,
          chainlink: null,
          upPred: tipHit.mean,
          upPredLo: tipHit.std != null ? Math.max(0, tipHit.mean - tipHit.std) : tipHit.mean,
          upPredHi: tipHit.std != null ? Math.min(1, tipHit.mean + tipHit.std) : tipHit.mean,
        })
      }

      for (let s = 1; s <= FORECAST_HORIZON_S; s++) {
        const hit = sampleAt(s)
        if (!hit) continue
        upsert({
          t: lastT + s * 1000,
          up: null,
          down: null,
          btc: null,
          twap: null,
          chainlink: null,
          upPred: hit.mean,
          upPredLo: hit.std != null ? Math.max(0, hit.mean - hit.std) : null,
          upPredHi: hit.std != null ? Math.min(1, hit.mean + hit.std) : null,
        })
      }
    }

    return [...byT.values()].sort((a, b) => a.t - b.t)
  }, [series, result?.history, result?.predictions])

  const nowMs = updatedAt ?? Date.now()
  /** Future pane ends at 5s ahead (small pad so the 5s tip isn't clipped). */
  const maxHorizonMs = 5_000 + 1_000

  const liveTipT = useMemo(() => {
    for (let i = series.length - 1; i >= 0; i--) {
      if (Number.isFinite(series[i].t)) return series[i].t
    }
    return nowMs
  }, [series, nowMs])

  const xDefaultDomain = useMemo((): TimeDomain => {
    const tip = liveTipT
    const futureSpan = maxHorizonMs
    const pastSpan = Math.max(30_000, MARKET_WINDOW_MS - futureSpan)
    return [tip - pastSpan, tip + futureSpan]
  }, [liveTipT, maxHorizonMs])

  const xFullDomain = useMemo((): TimeDomain => {
    const times = chartData.map((p) => p.t).filter((t) => Number.isFinite(t))
    const dataStart = times.length ? Math.min(...times) : liveTipT - MARKET_WINDOW_MS
    const dataEnd = times.length ? Math.max(...times) : liveTipT
    return [
      Math.min(dataStart, xDefaultDomain[0]),
      Math.max(dataEnd, xDefaultDomain[1], liveTipT + maxHorizonMs),
    ]
  }, [chartData, liveTipT, maxHorizonMs, xDefaultDomain])

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

  useEffect(() => {
    setFollowLiveX(true)
    setChartXDomain(null)
  }, [result?.market_id])

  const sharedXDomain = followLiveX ? xDefaultDomain : (chartXDomain ?? xDefaultDomain)
  const upPrice = lastFinite(series, 'up')
  const downPrice = lastFinite(series, 'down')
  const primary = result?.predictions.find((p) => p.horizon_seconds === 3) ?? result?.predictions[0] ?? null

  const onXDomainChange = (next: TimeDomain) => {
    setFollowLiveX(false)
    setChartXDomain(next)
  }
  const onXDomainReset = () => {
    setFollowLiveX(true)
    setChartXDomain(xDefaultDomain)
  }
  const toggleHorizon = (horizon: number) => {
    setSelectedHorizons((current) =>
      current.includes(horizon) ? current.filter((h) => h !== horizon) : [...current, horizon].sort((a, b) => a - b),
    )
  }
  const saveModelSelection = async () => {
    const horizons =
      modelKind === 'beta' ? parseHorizonList(customHorizons) : selectedHorizons
    if (!horizons.length) {
      setModelError(
        modelKind === 'beta'
          ? 'Enter at least one horizon t in seconds (e.g. 1,3,5,10).'
          : 'Select at least one model for live scoring.',
      )
      return
    }
    setModelBusy(true)
    try {
      const selected = await api.setActiveDirectionModels(horizons, modelKind)
      const nextKind = selected.kind ?? modelKind
      const nextHorizons = selected.horizons ?? selected.active_horizons ?? horizons
      setLiveKind(nextKind)
      setSelectedHorizons(nextHorizons)
      setCustomHorizons(nextHorizons.join(','))
      setResult(null)
      setModels((current) =>
        current
          ? {
              ...current,
              active_horizons: nextHorizons,
              active_kind: nextKind,
            }
          : current,
      )
      setModelError(null)
    } catch (err) {
      setModelError(err instanceof Error ? err.message : String(err))
    } finally {
      setModelBusy(false)
    }
  }
  const runModelJob = async () => {
    setModelBusy(true)
    try {
      const job = await api.startDirectionModelJob({
        action: modelAction,
        kind: trainKind,
        horizon_seconds: Number(modelHorizon),
      })
      setModels((current) => (current ? { ...current, job } : current))
      setModelError(null)
    } catch (err) {
      setModelError(err instanceof Error ? err.message : String(err))
    } finally {
      setModelBusy(false)
    }
  }

  return (
    <div className="workspace prediction-workspace">
      <aside className="workspace-rail workspace-rail-left">
        <div className="control-sidebar control-sidebar-embedded prediction-model-sidebar">
          <div className="sidebar-section">
            <div className="sidebar-heading mode-heading">
              <span>Models</span>
              <span className="mode-current-pill">{liveKind === 'beta' ? 'Beta' : 'Direction'}</span>
            </div>
            <p className="sidebar-hint">
              {modelKind === 'beta'
                ? continuousReady
                  ? 'Continuous-t Beta ready: PDF(x|X,t) for any t. Enter horizons to plot.'
                  : 'Enter horizons t. Train “Beta continuous-t” below for a true continuous-time PDF.'
                : 'Choose family, select horizons, then apply to the live feed.'}
            </p>
            <label className="sidebar-label">Family</label>
            <select
              value={modelKind}
              onChange={(e) => {
                const next = e.target.value as 'direction' | 'beta'
                setModelKind(next)
                setSelectedHorizons([])
                setModels(null)
              }}
            >
              <option value="direction">Direction classifier</option>
              <option value="beta">Beta distribution</option>
            </select>
            {modelError && <p className="error">{modelError}</p>}
            {modelKind === 'beta' ? (
              <>
                <label className="sidebar-label">Horizons t (seconds)</label>
                <input
                  type="text"
                  value={customHorizons}
                  onChange={(e) => setCustomHorizons(e.target.value)}
                  placeholder="e.g. 1,3,5,10"
                  spellCheck={false}
                />
                <p className="sidebar-hint">
                  Trained:{' '}
                  {models?.models.length
                    ? models.models.map((m) => `${m.horizon_seconds}s`).join(', ')
                    : 'none yet — train below'}
                </p>
              </>
            ) : (
              <div className="prediction-model-list">
                {models?.models.map((model) => {
                  const metrics = model.evaluation ?? model.metrics
                  const active = selectedHorizons.includes(model.horizon_seconds)
                  return (
                    <label
                      className={`prediction-model-card${active ? ' is-active' : ''}`}
                      key={model.id}
                    >
                      <input
                        type="checkbox"
                        checked={active}
                        onChange={() => toggleHorizon(model.horizon_seconds)}
                      />
                      <div className="prediction-model-card-body">
                        <div className="prediction-model-card-top">
                          <strong>{model.horizon_seconds}s</strong>
                          <span>{active ? 'On' : 'Off'}</span>
                        </div>
                        <div className="prediction-model-metrics">
                          <span>
                            AUC <b>{typeof metrics?.auc === 'number' ? metrics.auc.toFixed(3) : '—'}</b>
                          </span>
                          <span>
                            Acc{' '}
                            <b>
                              {typeof metrics?.accuracy === 'number'
                                ? formatPct(metrics.accuracy)
                                : '—'}
                            </b>
                          </span>
                        </div>
                      </div>
                    </label>
                  )
                })}
                {!models?.models.length && <p className="muted">No trained models found.</p>}
              </div>
            )}
            <button
              type="button"
              className="sidebar-btn primary full"
              disabled={
                modelBusy ||
                (modelKind === 'beta'
                  ? !parseHorizonList(customHorizons).length
                  : !selectedHorizons.length)
              }
              onClick={() => void saveModelSelection()}
            >
              Apply to live feed
            </button>
          </div>

          <div className="sidebar-section sidebar-section-last">
            <div className="sidebar-heading">Train / test</div>
            <label className="sidebar-label">Model</label>
            <select
              value={trainKind}
              onChange={(e) => setTrainKind(e.target.value as 'direction' | 'beta' | 'beta_ct')}
            >
              <option value="beta_ct">Beta continuous-t (any t)</option>
              <option value="beta">Beta fixed horizon</option>
              <option value="direction">Direction classifier</option>
            </select>
            <label className="sidebar-label">Action</label>
            <select value={modelAction} onChange={(e) => setModelAction(e.target.value as 'train' | 'evaluate')}>
              <option value="evaluate">Test saved model</option>
              <option value="train">Train model</option>
            </select>
            {trainKind !== 'beta_ct' ? (
              <>
                <label className="sidebar-label">Horizon</label>
                <select value={modelHorizon} onChange={(e) => setModelHorizon(e.target.value)}>
                  {[...new Set([3, 5, ...(models?.models.map((m) => m.horizon_seconds) ?? [])])]
                    .sort((a, b) => a - b)
                    .map((h) => (
                      <option key={h} value={h}>
                        {h}s
                      </option>
                    ))}
                </select>
              </>
            ) : (
              <p className="sidebar-hint">
                Learns μ(X,t), σ²(X,t) for t ∈ [0.5s, 30s]. Status: {continuousReady ? 'ready' : 'not trained'}
              </p>
            )}
            <button
              type="button"
              className="sidebar-btn primary full"
              disabled={modelBusy || models?.job?.status === 'running'}
              onClick={() => void runModelJob()}
            >
              {models?.job?.status === 'running'
                ? 'Running…'
                : modelAction === 'train'
                  ? 'Start training'
                  : 'Run test'}
            </button>
            {models?.job && (
              <div className={`prediction-job-panel prediction-job-${models.job.status}`}>
                <div className="prediction-job-top">
                  <span>
                    {models.job.kind ?? 'direction'} · {models.job.action}
                    {models.job.kind === 'beta_ct' ? '' : ` · ${models.job.horizon_seconds}s`}
                  </span>
                  <strong>{models.job.status}</strong>
                </div>
                <div className="prediction-job-bar" aria-hidden>
                  <span style={{ width: `${Math.max(4, Math.min(100, models.job.progress || 0))}%` }} />
                </div>
                <p>{models.job.message}</p>
              </div>
            )}
          </div>
        </div>
      </aside>

      <main className="workspace-main prediction-page">
        <header className="prediction-header">
          <div>
            <p className="eyebrow">Prediction desk</p>
            <h1>Up / Down</h1>
            <div className="mode-segment" role="group" aria-label="Market series" style={{ marginTop: 10 }}>
              <button
                type="button"
                className={`mode-segment-btn${marketSeries === '5m' ? ' active' : ''}`}
                onClick={() => setMarketSeries('5m')}
              >
                BTC 5m
              </button>
              <button
                type="button"
                className={`mode-segment-btn${marketSeries === '15m' ? ' active' : ''}`}
                onClick={() => setMarketSeries('15m')}
              >
                BTC 15m
              </button>
              <button
                type="button"
                className={`mode-segment-btn${marketSeries === 'bnb-15m' ? ' active' : ''}`}
                onClick={() => setMarketSeries('bnb-15m')}
              >
                BNB 15m
              </button>
            </div>
          </div>
          <div className="prediction-status-strip">
            <span className="prediction-live-dot" aria-hidden />
            <span>Live WS · {seriesLabel(marketSeries)}</span>
            <span className="prediction-status-sep" />
            <span>Market {result?.market_id ?? '—'}</span>
            <span className="prediction-status-sep" />
            <span>{result ? ageLabel(result.age_ms) : 'warming up'}</span>
            <span className="prediction-status-sep" />
            <span>Coverage {result ? formatPct(result.feature_coverage) : '—'}</span>
            <span className="prediction-status-sep" />
            <span>{liveKind === 'beta' ? 'Beta density' : 'Direction'}</span>
          </div>
        </header>

        {error && <p className="error prediction-error">{error}</p>}
        {!result && !error && !series.length && (
          <div className="panel prediction-loading muted">Connecting to live model feed…</div>
        )}

        {(result || series.length > 0) && (
          <>
            <div className="prediction-glance">
              <div className="prediction-price-now">
                <div className="prediction-section-label">Spot</div>
                <div className="prediction-price-now-row">
                  <div className="prediction-price-pill prediction-up">
                    <span>Up</span>
                    <strong>{formatCentsTrade(upPrice)}</strong>
                  </div>
                  <div className="prediction-price-pill prediction-down">
                    <span>Down</span>
                    <strong>{formatCentsTrade(downPrice)}</strong>
                  </div>
                </div>
              </div>

              <div className="prediction-chips">
                {result ? (
                  result.predictions.map((prediction) => (
                    <PredictionChip
                      key={prediction.horizon_seconds}
                      prediction={prediction}
                      kind={liveKind}
                    />
                  ))
                ) : (
                  <div className="prediction-chip prediction-chip-empty muted">Waiting for model score…</div>
                )}
              </div>

              <div
                className={`prediction-callout ${
                  primary ? (primary.direction === 'UP' ? 'prediction-up' : 'prediction-down') : ''
                }`}
              >
                <div className="prediction-section-label">Signal</div>
                {primary ? (
                  <>
                    <strong>
                      {liveKind === 'beta' && primary.mean != null
                        ? `μ ${formatCentsTrade(primary.mean)}`
                        : `${primary.direction} ${formatPct(
                            primary.direction === 'UP' ? primary.probability_up : primary.probability_down,
                          )}`}
                    </strong>
                    <em>
                      {primary.horizon_seconds}s · {liveKind === 'beta' ? 'Beta' : 'Direction'}
                    </em>
                  </>
                ) : (
                  <>
                    <strong>—</strong>
                    <em>warming up</em>
                  </>
                )}
              </div>
            </div>

            <div className="prediction-charts-row">
              <div className="panel prediction-chart-panel">
                <PriceChart
                  data={chartData}
                  mode="outcomes"
                  title="Up / Down · predicted"
                  xDomain={sharedXDomain}
                  onXDomainChange={onXDomainChange}
                  onXDomainReset={onXDomainReset}
                  xFullDomain={xFullDomain}
                  xDefaultDomain={xDefaultDomain}
                  followLive={followLiveX}
                  showFollowLive={!followLiveX}
                  onFollowLive={onXDomainReset}
                  hoverTime={hoverTime}
                  onHoverTimeChange={setHoverTime}
                  highlightTime={liveTipT}
                  bridgeOutcomeGaps
                />
              </div>
              <div className="panel prediction-chart-panel">
                <PredictionDistChart
                  distribution={result?.distribution ?? null}
                  distributions={result?.distributions ?? null}
                  predictions={result?.predictions ?? null}
                  title="Predicted Up density"
                />
              </div>
            </div>

            <footer className="prediction-footer">
              <span>Model {result?.model_kind ?? liveKind}</span>
              <span>
                Dist{' '}
                {(result?.distributions?.length
                  ? result.distributions
                  : result?.distribution
                    ? [result.distribution]
                    : []
                )
                  .map((d) => `${d.horizon_seconds}s`)
                  .join('+') || '—'}
              </span>
              {(result?.distributions?.length
                ? result.distributions
                : result?.distribution
                  ? [result.distribution]
                  : []
              ).map((d) => (
                <span key={d.horizon_seconds}>
                  {d.horizon_seconds}s μ {formatCentsTrade(d.mean)}
                </span>
              ))}
              <span>
                Updated {updatedAt ? new Date(updatedAt).toLocaleTimeString() : '—'}
              </span>
            </footer>
          </>
        )}
      </main>
    </div>
  )
}
