import { useEffect, useMemo, useRef, useState } from 'react'
import {
  formatCentsTrade,
  formatPct,
  wsUrl,
  type DirectionPrediction,
  type LiveDirectionPrediction,
  type LiveSeriesPoint,
} from '../api'
import PredictionProbChart, { type PredictionPoint } from '../components/PredictionProbChart'
import PriceChart, { type TimeDomain } from '../components/PriceChart'

const MARKET_WINDOW_MS = 300_000
const MAX_PRED_POINTS = 900
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
  if (!prev.length) return [point]
  const last = prev[prev.length - 1]
  if (last.t === point.t) {
    return [
      ...prev.slice(0, -1),
      {
        ...last,
        ...point,
        up: mergePrice(point.up, last.up),
        down: mergePrice(point.down, last.down),
        btc: mergePrice(point.btc, last.btc),
        twap: mergePrice(point.twap, last.twap),
        chainlink: mergePrice(point.chainlink, last.chainlink),
      },
    ]
  }
  const next = [...prev, point]
  return next.length > MAX_SERIES_POINTS ? next.slice(-MAX_SERIES_POINTS) : next
}

function predictionPointsFromResult(result: LiveDirectionPrediction): PredictionPoint[] {
  const points: PredictionPoint[] = []
  for (const row of result.history ?? []) {
    const t = num(row.timestamp)
    if (t == null) continue
    const p3 = num(row.p_up_3s)
    const p5 = num(row.p_up_5s)
    if (p3 == null && p5 == null) continue
    points.push({ t, p_up_3s: p3, p_up_5s: p5 })
  }
  if (points.length) return points.sort((a, b) => a.t - b.t)

  const byHorizon = new Map(
    result.predictions.map((p) => [Number(p.horizon_seconds), Number(p.probability_up)] as const),
  )
  const t = num(result.timestamp)
  if (t == null) return []
  return [{ t, p_up_3s: num(byHorizon.get(3)), p_up_5s: num(byHorizon.get(5)) }]
}

function PredictionChip({ prediction }: { prediction: DirectionPrediction }) {
  const isUp = prediction.direction === 'UP'
  const probability = isUp ? prediction.probability_up : prediction.probability_down
  return (
    <article className={`prediction-chip ${isUp ? 'prediction-up' : 'prediction-down'}`}>
      <div className="prediction-chip-header">
        <span>{prediction.horizon_seconds}s</span>
        <strong>{prediction.direction}</strong>
      </div>
      <div className="prediction-chip-value">{formatPct(probability)}</div>
      <div
        className="prediction-bar"
        aria-label={`Up ${formatPct(prediction.probability_up)}, Down ${formatPct(prediction.probability_down)}`}
      >
        <span className="prediction-bar-up" style={{ width: `${prediction.probability_up * 100}%` }} />
        <span className="prediction-bar-down" style={{ width: `${prediction.probability_down * 100}%` }} />
      </div>
      <p className="muted">Conf {formatPct(prediction.confidence)}</p>
    </article>
  )
}

export default function PredictionPage() {
  const [result, setResult] = useState<LiveDirectionPrediction | null>(null)
  const [series, setSeries] = useState<LiveSeriesPoint[]>([])
  const [predSeries, setPredSeries] = useState<PredictionPoint[]>([])
  const [error, setError] = useState<string | null>(null)
  const [updatedAt, setUpdatedAt] = useState<number | null>(null)
  const [chartXDomain, setChartXDomain] = useState<TimeDomain | null>(null)
  const [followLiveX, setFollowLiveX] = useState(true)
  const [hoverTime, setHoverTime] = useState<number | null>(null)
  const marketIdRef = useRef<string | null>(null)
  const wsRef = useRef<WebSocket | null>(null)

  useEffect(() => {
    let cancelled = false
    let reconnectTimer: number | null = null

    const clearReconnect = () => {
      if (reconnectTimer != null) {
        window.clearTimeout(reconnectTimer)
        reconnectTimer = null
      }
    }

    const applyPrediction = (prediction: LiveDirectionPrediction) => {
      if (marketIdRef.current && marketIdRef.current !== prediction.market_id) {
        setPredSeries([])
      }
      marketIdRef.current = prediction.market_id
      setResult(prediction)
      const nextPoints = predictionPointsFromResult(prediction)
      if (nextPoints.length) {
        setPredSeries(
          nextPoints.length > MAX_PRED_POINTS ? nextPoints.slice(-MAX_PRED_POINTS) : nextPoints,
        )
      }
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
        ws.send(JSON.stringify({ interval_s: 0.5, want_direction: true }))
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
          applyPrediction(msg as unknown as LiveDirectionPrediction)
          return
        }
        if (type === 'series') {
          const points = mapSeriesPoints(msg.series as LiveSeriesPoint[] | undefined)
          if (points.length) setSeries((prev) => mergeSeriesSeed(prev, points))
          return
        }
        if (type === 'market') {
          const mid = msg.market_id != null ? String(msg.market_id) : null
          if (mid && marketIdRef.current && marketIdRef.current !== mid) {
            setSeries([])
            setPredSeries([])
            setResult(null)
          }
          if (mid) marketIdRef.current = mid
          return
        }
        if (type === 'tick') {
          if (msg.error) setError(String(msg.error))
          else setError(null)
          if (msg.market_id) marketIdRef.current = String(msg.market_id)
          setSeries((prev) =>
            appendTickPoint(prev, {
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
  }, [])

  const chartData = useMemo(
    () =>
      series.map((p) => ({
        t: p.t,
        up: p.up ?? null,
        down: p.down ?? null,
        btc: p.btc ?? null,
        twap: p.twap ?? null,
        chainlink: p.chainlink ?? null,
      })),
    [series],
  )

  const nowMs = updatedAt ?? Date.now()
  const xFullDomain = useMemo((): TimeDomain => {
    const times = [
      ...chartData.map((p) => p.t),
      ...predSeries.map((p) => p.t),
    ].filter((t) => Number.isFinite(t))
    if (times.length >= 2) return [Math.min(...times), Math.max(...times)]
    if (times.length === 1) return [times[0], times[0] + MARKET_WINDOW_MS]
    return [nowMs - MARKET_WINDOW_MS, nowMs]
  }, [chartData, predSeries, nowMs])

  const xDefaultDomain = useMemo((): TimeDomain => {
    const [, end] = xFullDomain
    const start = Math.max(xFullDomain[0], end - MARKET_WINDOW_MS)
    return [start, end]
  }, [xFullDomain])

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

  return (
    <section className="prediction-page">
      <div className="page-heading">
        <div>
          <p className="eyebrow">Live model</p>
          <h1>Up / Down movement</h1>
          <p className="muted">
            Price ticks and model scores stream over WebSocket (~0.5s). No HTTP polling.
          </p>
        </div>
        {result && (
          <div className="prediction-meta">
            <span>Market {result.market_id}</span>
            <span>Data {ageLabel(result.age_ms)}</span>
          </div>
        )}
      </div>

      {error && <p className="error">{error}</p>}
      {!result && !error && !series.length && (
        <div className="panel muted">Loading latest model prediction…</div>
      )}

      {(result || series.length > 0) && (
        <>
          <div className="prediction-glance">
            <div className="prediction-price-now">
              <div className="prediction-price-now-label">Current</div>
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
                  <PredictionChip key={prediction.horizon_seconds} prediction={prediction} />
                ))
              ) : (
                <div className="panel muted" style={{ gridColumn: '1 / -1' }}>
                  Waiting for model score…
                </div>
              )}
            </div>

            {primary ? (
              <div className={`prediction-callout ${primary.direction === 'UP' ? 'prediction-up' : 'prediction-down'}`}>
                <span>Next move</span>
                <strong>
                  {primary.direction} ·{' '}
                  {formatPct(primary.direction === 'UP' ? primary.probability_up : primary.probability_down)}
                </strong>
                <em>{primary.horizon_seconds}s model</em>
              </div>
            ) : (
              <div className="prediction-callout">
                <span>Next move</span>
                <strong>—</strong>
                <em>warming up</em>
              </div>
            )}
          </div>

          <div className="prediction-charts-row">
            <div className="panel prediction-chart-panel">
              <PriceChart
                data={chartData}
                mode="outcomes"
                title="Up / Down price"
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
              />
            </div>
            <div className="panel prediction-chart-panel">
              <PredictionProbChart
                data={predSeries}
                xDomain={sharedXDomain}
                hoverTime={hoverTime}
                onHoverTimeChange={setHoverTime}
                title="Prediction · P(Up move)"
              />
            </div>
          </div>

          <div className="panel prediction-note">
            <strong>How to read this</strong>
            <p>
              Left chart is live Up/Down mid. Right chart is model P(Up) for 3s (solid) and 5s (dashed).
              Above 50% leans Up; below 50% leans Down.
            </p>
            <p className="muted">
              Feature coverage: {result ? formatPct(result.feature_coverage) : '—'} · Prediction samples:{' '}
              {predSeries.length} · Last WS update:{' '}
              {updatedAt ? new Date(updatedAt).toLocaleTimeString() : '—'}
            </p>
          </div>
        </>
      )}
    </section>
  )
}
