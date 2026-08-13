import { useEffect, useMemo, useState } from 'react'
import {
  api,
  type LgbmModelInfo,
  type LgbmTrainJob,
  type StrategyCatalogItem,
} from '../api'

function fmtNum(v: unknown, digits = 4): string {
  if (v == null || v === '') return '—'
  const n = typeof v === 'number' ? v : Number(v)
  if (!Number.isFinite(n)) return String(v)
  return n.toFixed(digits)
}

function ParamTable({ params }: { params: Record<string, unknown> }) {
  const entries = Object.entries(params || {})
  if (!entries.length) {
    return <p className="muted strategy-empty">No parameters</p>
  }
  return (
    <table className="strategy-param-table">
      <thead>
        <tr>
          <th>Parameter</th>
          <th>Value</th>
        </tr>
      </thead>
      <tbody>
        {entries.map(([k, v]) => (
          <tr key={k}>
            <td>
              <code>{k}</code>
            </td>
            <td>
              <code>{typeof v === 'string' ? v : JSON.stringify(v)}</code>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function MetricsBlock({ metrics }: { metrics: Record<string, unknown> | null | undefined }) {
  if (!metrics || metrics.error) {
    return (
      <p className="muted strategy-empty">
        {metrics?.error ? String(metrics.error) : 'No metrics.json yet — train a model first.'}
      </p>
    )
  }
  const splits = ['train', 'validation', 'test'] as const
  const importance = Array.isArray(metrics.feature_importance_gain_top20)
    ? (metrics.feature_importance_gain_top20 as { feature: string; gain: number }[])
    : []
  const trainedParams =
    metrics.params && typeof metrics.params === 'object'
      ? (metrics.params as Record<string, unknown>)
      : null

  return (
    <div className="strategy-metrics">
      <div className="strategy-metric-grid">
        <div className="strategy-metric-card">
          <div className="strategy-metric-label">Best iteration</div>
          <div className="strategy-metric-value">{String(metrics.best_iteration ?? '—')}</div>
        </div>
        <div className="strategy-metric-card">
          <div className="strategy-metric-label">Features</div>
          <div className="strategy-metric-value">{String(metrics.n_features ?? '—')}</div>
        </div>
        {splits.map((split) => {
          const row = metrics[split] as { logloss?: number; auc?: number } | undefined
          const nRows =
            metrics.n_rows && typeof metrics.n_rows === 'object'
              ? (metrics.n_rows as Record<string, number>)[split]
              : undefined
          return (
            <div key={split} className="strategy-metric-card">
              <div className="strategy-metric-label">{split}</div>
              <div className="strategy-metric-value">
                AUC {fmtNum(row?.auc, 4)}
              </div>
              <div className="muted strategy-metric-sub">
                logloss {fmtNum(row?.logloss, 5)}
                {nRows != null ? ` · ${nRows.toLocaleString()} rows` : ''}
              </div>
            </div>
          )
        })}
      </div>

      {trainedParams && (
        <div className="strategy-section">
          <h3>Trained LightGBM params</h3>
          <ParamTable params={trainedParams} />
        </div>
      )}

      {importance.length > 0 && (
        <div className="strategy-section">
          <h3>Top features (gain)</h3>
          <table className="strategy-param-table">
            <thead>
              <tr>
                <th>#</th>
                <th>Feature</th>
                <th>Gain</th>
              </tr>
            </thead>
            <tbody>
              {importance.slice(0, 15).map((row, i) => (
                <tr key={row.feature}>
                  <td>{i + 1}</td>
                  <td>
                    <code>{row.feature}</code>
                  </td>
                  <td>{fmtNum(row.gain, 1)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

export default function StrategyPage() {
  const [catalog, setCatalog] = useState<StrategyCatalogItem[]>([])
  const [selected, setSelected] = useState('lgbm_edge')
  const [model, setModel] = useState<LgbmModelInfo | null>(null)
  const [job, setJob] = useState<LgbmTrainJob | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [training, setTraining] = useState(false)

  const [numBoostRound, setNumBoostRound] = useState(500)
  const [earlyStopping, setEarlyStopping] = useState(50)
  const [maxMarkets, setMaxMarkets] = useState('')

  const active = useMemo(
    () => catalog.find((s) => s.name === selected) || catalog[0] || null,
    [catalog, selected],
  )

  const refreshModel = async () => {
    const info = await api.lgbmModel()
    setModel(info)
    if (info.train_job) setJob(info.train_job)
  }

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    Promise.all([api.strategiesCatalog(), api.lgbmModel()])
      .then(([cat, info]) => {
        if (cancelled) return
        setCatalog(cat.strategies || [])
        setModel(info)
        setJob(info.train_job || null)
        const defs = cat.strategies?.find((s) => s.name === 'lgbm_edge')?.train_defaults
        if (defs) {
          if (typeof defs.num_boost_round === 'number') setNumBoostRound(defs.num_boost_round)
          if (typeof defs.early_stopping_rounds === 'number') {
            setEarlyStopping(defs.early_stopping_rounds)
          }
        }
        if (cat.strategies?.length && !cat.strategies.some((s) => s.name === selected)) {
          setSelected(cat.strategies[0].name)
        }
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    if (job?.status !== 'running') return
    const id = window.setInterval(() => {
      void api
        .lgbmTrainStatus()
        .then((j) => {
          setJob(j)
          if (j.status !== 'running') void refreshModel().catch(() => undefined)
        })
        .catch(() => undefined)
    }, 2000)
    return () => window.clearInterval(id)
  }, [job?.status])

  const startTrain = async () => {
    setError(null)
    setTraining(true)
    try {
      const body: {
        num_boost_round: number
        early_stopping_rounds: number
        max_markets?: number | null
      } = {
        num_boost_round: numBoostRound,
        early_stopping_rounds: earlyStopping,
      }
      const mm = maxMarkets.trim()
      if (mm) body.max_markets = Number(mm)
      const res = await api.lgbmTrain(body)
      setJob(res.job)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setTraining(false)
    }
  }

  const isLgbm = active?.name === 'lgbm_edge'
  const runtimeParams = active?.runtime_params || active?.params || {}

  return (
    <div className="strategy-page">
      <div className="strategy-hero">
        <div>
          <h1>Strategy</h1>
          <p className="muted">
            Browse strategy ideas, required data, runtime parameters, and train the LightGBM baseline
            used by backtest / paper.
          </p>
        </div>
      </div>

      {error && <div className="strategy-error">{error}</div>}
      {loading && <p className="muted">Loading strategies…</p>}

      <div className="strategy-layout">
        <aside className="strategy-rail panel">
          <div className="strategy-rail-head">Strategies</div>
          <ul className="strategy-list">
            {catalog.map((s) => (
              <li key={s.name}>
                <button
                  type="button"
                  className={`strategy-list-item${active?.name === s.name ? ' active' : ''}`}
                  onClick={() => setSelected(s.name)}
                >
                  <strong>{s.title || s.name}</strong>
                  <span className="muted">{s.name}</span>
                  {s.trainable && <span className="strategy-pill">trainable</span>}
                </button>
              </li>
            ))}
          </ul>
        </aside>

        <div className="strategy-main">
          {active && (
            <>
              <section className="panel strategy-panel">
                <div className="strategy-panel-head">
                  <h2>{active.title || active.name}</h2>
                  <code className="strategy-code-tag">{active.name}</code>
                </div>
                <div className="strategy-section">
                  <h3>Idea</h3>
                  <p>{active.idea || active.description}</p>
                  {active.when_to_use && (
                    <p className="muted strategy-when">
                      <strong>When:</strong> {active.when_to_use}
                    </p>
                  )}
                </div>

                <div className="strategy-section">
                  <h3>Data required</h3>
                  {(active.data_required || []).length === 0 ? (
                    <p className="muted strategy-empty">No special dataset beyond live ticks.</p>
                  ) : (
                    <ul className="strategy-data-list">
                      {(active.data_required || []).map((d) => (
                        <li key={d.name}>
                          <strong>{d.name}</strong>
                          <code>{d.path}</code>
                          <span className="muted">{d.why}</span>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>

                <div className="strategy-section">
                  <h3>Runtime parameters</h3>
                  <ParamTable params={runtimeParams} />
                </div>

                {(active.outputs || []).length > 0 && (
                  <div className="strategy-section">
                    <h3>Artifacts</h3>
                    <ul className="strategy-outputs">
                      {(active.outputs || []).map((o) => (
                        <li key={o}>
                          <code>{o}</code>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </section>

              {isLgbm && (
                <section className="panel strategy-panel">
                  <div className="strategy-panel-head">
                    <h2>Train LightGBM</h2>
                    <button
                      type="button"
                      className="strategy-refresh"
                      onClick={() => void refreshModel().catch((e) => setError(String(e)))}
                    >
                      Refresh status
                    </button>
                  </div>

                  <div className="strategy-section">
                    <h3>Dataset status</h3>
                    <div className="strategy-split-grid">
                      {model &&
                        Object.entries(model.splits || {}).map(([split, info]) => (
                          <div key={split} className="strategy-split-card">
                            <div className="strategy-metric-label">{split}</div>
                            <div className="strategy-metric-value">
                              {info.exists ? `${info.n_markets} markets` : 'missing'}
                            </div>
                            <div className="muted strategy-metric-sub" title={info.path}>
                              {info.path}
                            </div>
                          </div>
                        ))}
                    </div>
                    <p className="muted strategy-path-line">
                      Model:{' '}
                      <code>
                        {model?.model_exists ? 'present' : 'missing'} · {model?.model_path}
                      </code>
                      {model?.model_mtime ? ` · mtime ${model.model_mtime}` : ''}
                    </p>
                    <p className="muted strategy-path-line">
                      Schema features: {model?.n_schema_features ?? '—'}
                    </p>
                  </div>

                  <div className="strategy-section">
                    <h3>Train parameters</h3>
                    <div className="strategy-train-form">
                      <label>
                        num_boost_round
                        <input
                          type="number"
                          min={10}
                          max={5000}
                          value={numBoostRound}
                          onChange={(e) => setNumBoostRound(Number(e.target.value) || 500)}
                        />
                      </label>
                      <label>
                        early_stopping_rounds
                        <input
                          type="number"
                          min={1}
                          max={500}
                          value={earlyStopping}
                          onChange={(e) => setEarlyStopping(Number(e.target.value) || 50)}
                        />
                      </label>
                      <label>
                        max_markets (optional debug)
                        <input
                          type="number"
                          min={1}
                          placeholder="all"
                          value={maxMarkets}
                          onChange={(e) => setMaxMarkets(e.target.value)}
                        />
                      </label>
                      <button
                        type="button"
                        className="strategy-train-btn"
                        disabled={training || job?.status === 'running'}
                        onClick={() => void startTrain()}
                      >
                        {job?.status === 'running' || training ? 'Training…' : 'Start training'}
                      </button>
                    </div>
                    {job && (
                      <div className={`strategy-job strategy-job-${job.status}`}>
                        <div>
                          Status: <strong>{job.status}</strong>
                          {job.pid != null ? ` · pid ${job.pid}` : ''}
                        </div>
                        {job.started_at && (
                          <div className="muted">Started {job.started_at}</div>
                        )}
                        {job.finished_at && (
                          <div className="muted">Finished {job.finished_at}</div>
                        )}
                        {job.error && <div className="strategy-error">{job.error}</div>}
                        {(job.log_tail || []).length > 0 && (
                          <pre className="strategy-log">{(job.log_tail || []).join('\n')}</pre>
                        )}
                      </div>
                    )}
                  </div>

                  <div className="strategy-section">
                    <h3>Last trained metrics</h3>
                    <MetricsBlock metrics={model?.metrics || job?.metrics || null} />
                  </div>

                  {!!(model?.schema_features || []).length && (
                    <div className="strategy-section">
                      <h3>Feature columns ({model?.schema_features?.length})</h3>
                      <div className="strategy-feature-chips">
                        {(model?.schema_features || []).map((f) => (
                          <code key={f}>{f}</code>
                        ))}
                      </div>
                    </div>
                  )}
                </section>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  )
}
