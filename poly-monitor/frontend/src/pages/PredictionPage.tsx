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

const MARKET_WINDOW_MS = 300_000
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
  const [modelAction, setModelAction] = useState<'train' | 'evaluate'>('evaluate')
  const [modelKind, setModelKind] = useState<'direction' | 'beta'>('direction')
  const [modelHorizon, setModelHorizon] = useState('3')
  const [modelBusy, setModelBusy] = useState(false)
  const [modelError, setModelError] = useState<string | null>(null)
  const [liveKind, setLiveKind] = useState<'direction' | 'beta'>('direction')
  const marketIdRef = useRef<string | null>(null)
  const wsRef = useRef<WebSocket | null>(null)
  const kindBootstrapped = useRef(false)

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
        setLiveKind(next.active_kind ?? 'direction')
        setSelectedHorizons((current) => {
          if (next.active_kind === modelKind && next.active_horizons.length) {
            return next.active_horizons
          }
          if (current.length && current.every((h) => next.models.some((m) => m.horizon_seconds === h))) {
            return current
          }
          return next.models.map((m) => m.horizon_seconds)
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

    const clearReconnect = () => {
      if (reconnectTimer != null) {
        window.clearTimeout(reconnectTimer)
        reconnectTimer = null
      }
    }

    const applyPrediction = (prediction: LiveDirectionPrediction) => {
      marketIdRef.current = prediction.market_id
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
          const prediction = msg as unknown as LiveDirectionPrediction
          if (prediction.model_kind === 'beta' || prediction.model_kind === 'direction') {
            setLiveKind(prediction.model_kind)
          }
          applyPrediction(prediction)
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
    const times = chartData.map((p) => p.t).filter((t) => Number.isFinite(t))
    if (times.length >= 2) return [Math.min(...times), Math.max(...times)]
    if (times.length === 1) return [times[0], times[0] + MARKET_WINDOW_MS]
    return [nowMs - MARKET_WINDOW_MS, nowMs]
  }, [chartData, nowMs])

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
  const toggleHorizon = (horizon: number) => {
    setSelectedHorizons((current) =>
      current.includes(horizon) ? current.filter((h) => h !== horizon) : [...current, horizon].sort((a, b) => a - b),
    )
  }
  const saveModelSelection = async () => {
    if (!selectedHorizons.length) {
      setModelError('Select at least one model for live scoring.')
      return
    }
    setModelBusy(true)
    try {
      const selected = await api.setActiveDirectionModels(selectedHorizons, modelKind)
      const nextKind = selected.kind ?? modelKind
      setLiveKind(nextKind)
      setResult(null)
      setModels((current) =>
        current
          ? {
              ...current,
              active_horizons: selected.horizons ?? selected.active_horizons ?? selectedHorizons,
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
        kind: modelKind,
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
            <p className="sidebar-hint">Choose family, select horizons, then apply to the live feed.</p>
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
                        {modelKind === 'beta' ? (
                          <>
                            <span>
                              MAE{' '}
                              <b>
                                {typeof metrics?.mae === 'number' ? metrics.mae.toFixed(4) : '—'}
                              </b>
                            </span>
                            <span>
                              NLL{' '}
                              <b>
                                {typeof metrics?.beta_nll === 'number'
                                  ? metrics.beta_nll.toFixed(3)
                                  : '—'}
                              </b>
                            </span>
                          </>
                        ) : (
                          <>
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
                          </>
                        )}
                      </div>
                    </div>
                  </label>
                )
              })}
              {!models?.models.length && (
                <p className="muted">
                  {modelKind === 'beta'
                    ? 'No Beta models yet — train one below.'
                    : 'No trained models found.'}
                </p>
              )}
            </div>
            <button
              type="button"
              className="sidebar-btn primary full"
              disabled={modelBusy || !selectedHorizons.length}
              onClick={() => void saveModelSelection()}
            >
              Apply to live feed
            </button>
          </div>

          <div className="sidebar-section sidebar-section-last">
            <div className="sidebar-heading">Train / test</div>
            <label className="sidebar-label">Action</label>
            <select value={modelAction} onChange={(e) => setModelAction(e.target.value as 'train' | 'evaluate')}>
              <option value="evaluate">Test saved model</option>
              <option value="train">Train model</option>
            </select>
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
                    {models.job.kind ?? 'direction'} · {models.job.action} · {models.job.horizon_seconds}s
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
          </div>
          <div className="prediction-status-strip">
            <span className="prediction-live-dot" aria-hidden />
            <span>Live WS</span>
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
