import { useEffect, useMemo, useState } from 'react'
import {
  api,
  formatCentsTrade,
  formatPct,
  type DirectionPrediction,
  type LiveDirectionPrediction,
  type LiveSeriesPoint,
} from '../api'
import PriceChart, { type TimeDomain } from '../components/PriceChart'

const REFRESH_MS = 2_000
const MARKET_WINDOW_MS = 300_000

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
  const [error, setError] = useState<string | null>(null)
  const [updatedAt, setUpdatedAt] = useState<number | null>(null)
  const [chartXDomain, setChartXDomain] = useState<TimeDomain | null>(null)
  const [followLiveX, setFollowLiveX] = useState(true)
  const [hoverTime, setHoverTime] = useState<number | null>(null)

  useEffect(() => {
    let cancelled = false
    const refresh = async () => {
      try {
        const [prediction, seriesRes] = await Promise.all([
          api.liveDirectionPrediction().catch((err) => {
            throw err
          }),
          api.liveSeries(undefined, MARKET_WINDOW_MS).catch(() => null),
        ])
        if (cancelled) return
        setResult(prediction)
        if (seriesRes?.series?.length) {
          setSeries(
            seriesRes.series
              .filter((p) => p.t != null && Number.isFinite(Number(p.t)))
              .map((p) => ({
                t: Number(p.t),
                up: p.up ?? null,
                down: p.down ?? null,
                btc: p.btc ?? null,
                twap: p.twap ?? null,
                chainlink: p.chainlink ?? null,
              })),
          )
        }
        setUpdatedAt(Date.now())
        setError(null)
      } catch (err) {
        if (cancelled) return
        // Keep chart alive even when prediction is warming up.
        try {
          const seriesRes = await api.liveSeries(undefined, MARKET_WINDOW_MS)
          if (!cancelled && seriesRes?.series?.length) {
            setSeries(
              seriesRes.series
                .filter((p) => p.t != null && Number.isFinite(Number(p.t)))
                .map((p) => ({
                  t: Number(p.t),
                  up: p.up ?? null,
                  down: p.down ?? null,
                  btc: p.btc ?? null,
                  twap: p.twap ?? null,
                  chainlink: p.chainlink ?? null,
                })),
            )
          }
        } catch {
          // ignore secondary series failure
        }
        setError(err instanceof Error ? err.message : String(err))
      }
    }
    void refresh()
    const id = window.setInterval(() => void refresh(), REFRESH_MS)
    return () => {
      cancelled = true
      window.clearInterval(id)
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
    if (chartData.length >= 2) return [chartData[0].t, chartData[chartData.length - 1].t]
    if (chartData.length === 1) return [chartData[0].t, chartData[0].t + MARKET_WINDOW_MS]
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

  return (
    <section className="prediction-page">
      <div className="page-heading">
        <div>
          <p className="eyebrow">Live model</p>
          <h1>Up / Down movement</h1>
          <p className="muted">
            Live prices and direction probabilities in one view. Refreshes every 2 seconds.
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
                  {primary.direction} · {formatPct(primary.direction === 'UP' ? primary.probability_up : primary.probability_down)}
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

          <div className="panel prediction-chart-panel">
            <PriceChart
              data={chartData}
              mode="outcomes"
              title="Up / Down price"
              xDomain={sharedXDomain}
              onXDomainChange={(next) => {
                setFollowLiveX(false)
                setChartXDomain(next)
              }}
              onXDomainReset={() => {
                setFollowLiveX(true)
                setChartXDomain(xDefaultDomain)
              }}
              xFullDomain={xFullDomain}
              xDefaultDomain={xDefaultDomain}
              followLive={followLiveX}
              showFollowLive={!followLiveX}
              onFollowLive={() => {
                setFollowLiveX(true)
                setChartXDomain(xDefaultDomain)
              }}
              hoverTime={hoverTime}
              onHoverTimeChange={setHoverTime}
            />
          </div>

          <div className="panel prediction-note">
            <strong>How to read this</strong>
            <p>
              Chart shows live Up/Down mids. Cards show the model&apos;s probability that the next non-flat
              Up-mid move is Up or Down (min 0.1¢). Not settlement, fills, or fees.
            </p>
            <p className="muted">
              Feature coverage: {result ? formatPct(result.feature_coverage) : '—'} · Last API refresh:{' '}
              {updatedAt ? new Date(updatedAt).toLocaleTimeString() : '—'}
            </p>
          </div>
        </>
      )}
    </section>
  )
}
