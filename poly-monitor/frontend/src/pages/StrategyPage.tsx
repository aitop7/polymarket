import { useEffect, useMemo, useState } from 'react'
import {
  api,
  type LgbmModelInfo,
  type LgbmTrainJob,
  type MomentumPairTrainJob,
  type StrategyCatalogItem,
  type StrategyVersionSummary,
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
              <div className="strategy-metric-value">AUC {fmtNum(row?.auc, 4)}</div>
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

function pretty(obj: Record<string, unknown>): string {
  return JSON.stringify(obj, null, 2)
}

export default function StrategyPage() {
  const [catalog, setCatalog] = useState<StrategyCatalogItem[]>([])
  const [selected, setSelected] = useState('lgbm_edge')
  const [model, setModel] = useState<LgbmModelInfo | null>(null)
  const [job, setJob] = useState<LgbmTrainJob | null>(null)
  const [mpJob, setMpJob] = useState<MomentumPairTrainJob | null>(null)
  const [versions, setVersions] = useState<StrategyVersionSummary[]>([])
  const [versionsDir, setVersionsDir] = useState('')
  const [activeVersionId, setActiveVersionId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [training, setTraining] = useState(false)
  const [saving, setSaving] = useState(false)
  const [loadingVersion, setLoadingVersion] = useState<string | null>(null)

  const [runtimeParamsText, setRuntimeParamsText] = useState('{}')
  const [versionLabel, setVersionLabel] = useState('')
  const [numBoostRound, setNumBoostRound] = useState(500)
  const [earlyStopping, setEarlyStopping] = useState(50)
  const [maxMarkets, setMaxMarkets] = useState('')
  const [horizonSeconds, setHorizonSeconds] = useState(5)
  const [deltaSeconds, setDeltaSeconds] = useState(1)
  const [trainRatio, setTrainRatio] = useState(0.8)

  const active = useMemo(
    () => catalog.find((s) => s.name === selected) || catalog[0] || null,
    [catalog, selected],
  )

  const refreshModel = async () => {
    const info = await api.lgbmModel()
    setModel(info)
    if (info.train_job) setJob(info.train_job)
  }

  const refreshVersions = async (name: string) => {
    const [list, activeRes] = await Promise.all([
      api.strategyVersions(name),
      api.strategyActiveVersion(name),
    ])
    setVersions(list.versions || [])
    setVersionsDir(list.dir || '')
    setActiveVersionId(list.active_version_id || null)
    const rp = activeRes.version?.runtime_params
    if (rp && typeof rp === 'object') {
      setRuntimeParamsText(pretty(rp as Record<string, unknown>))
    }
    const tp = activeRes.version?.train_params
    if (tp && typeof tp === 'object') {
      const t = tp as Record<string, unknown>
      if (typeof t.num_boost_round === 'number') setNumBoostRound(t.num_boost_round)
      if (typeof t.early_stopping_rounds === 'number') setEarlyStopping(t.early_stopping_rounds)
      if (t.max_markets != null && t.max_markets !== '') setMaxMarkets(String(t.max_markets))
    }
  }

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    Promise.all([
      api.strategiesCatalog(),
      api.lgbmModel(),
      api.momentumPairTrainStatus().catch(() => null),
    ])
      .then(async ([cat, info, mp]) => {
        if (cancelled) return
        setCatalog(cat.strategies || [])
        setModel(info)
        setJob(info.train_job || null)
        if (mp) setMpJob(mp)
        const first =
          cat.strategies?.find((s) => s.name === 'momentum_pair')?.name ||
          cat.strategies?.find((s) => s.name === 'lgbm_edge')?.name ||
          cat.strategies?.[0]?.name ||
          'lgbm_edge'
        setSelected(first)
        const defs = cat.strategies?.find((s) => s.name === first)
        setRuntimeParamsText(
          pretty((defs?.runtime_params || defs?.params || {}) as Record<string, unknown>),
        )
        const trainDefs = cat.strategies?.find((s) => s.name === first)?.train_defaults
        if (trainDefs) {
          if (typeof trainDefs.num_boost_round === 'number') setNumBoostRound(trainDefs.num_boost_round)
          if (typeof trainDefs.early_stopping_rounds === 'number') {
            setEarlyStopping(trainDefs.early_stopping_rounds)
          }
          if (typeof trainDefs.horizon_seconds === 'number') {
            setHorizonSeconds(trainDefs.horizon_seconds)
          }
          if (typeof trainDefs.train_ratio === 'number') setTrainRatio(trainDefs.train_ratio)
        }
        await refreshVersions(first)
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
  }, [])

  useEffect(() => {
    if (!selected || loading) return
    let cancelled = false
    setError(null)
    refreshVersions(selected).catch((e) => {
      if (!cancelled) setError(e instanceof Error ? e.message : String(e))
    })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected])

  useEffect(() => {
    if (job?.status !== 'running') return
    const id = window.setInterval(() => {
      void api
        .lgbmTrainStatus()
        .then((j) => {
          setJob(j)
          if (j.status !== 'running') {
            void refreshModel().catch(() => undefined)
            if (selected === 'lgbm_edge') void refreshVersions('lgbm_edge').catch(() => undefined)
          }
        })
        .catch(() => undefined)
    }, 2000)
    return () => window.clearInterval(id)
  }, [job?.status, selected])

  useEffect(() => {
    if (mpJob?.status !== 'running') return
    const id = window.setInterval(() => {
      void api
        .momentumPairTrainStatus()
        .then((j) => {
          setMpJob(j)
          if (j.status !== 'running') {
            void refreshVersions('momentum_pair').catch(() => undefined)
          }
        })
        .catch(() => undefined)
    }, 1000)
    return () => window.clearInterval(id)
  }, [mpJob?.status])

  const parseRuntimeParams = (): Record<string, unknown> => {
    const parsed = JSON.parse(runtimeParamsText) as unknown
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      throw new Error('Runtime params must be a JSON object')
    }
    return parsed as Record<string, unknown>
  }

  const saveParamsVersion = async () => {
    if (!active) return
    setSaving(true)
    setError(null)
    try {
      const runtime_params = parseRuntimeParams()
      let train_params: Record<string, unknown> = {}
      if (active.name === 'lgbm_edge') {
        train_params = {
          num_boost_round: numBoostRound,
          early_stopping_rounds: earlyStopping,
          max_markets: maxMarkets.trim() ? Number(maxMarkets) : null,
        }
      } else if (active.name === 'momentum_pair') {
        train_params = {
          horizon_seconds: horizonSeconds,
          delta_seconds: deltaSeconds,
          train_ratio: trainRatio,
          num_boost_round: numBoostRound,
          early_stopping_rounds: earlyStopping,
          max_markets: maxMarkets.trim() ? Number(maxMarkets) : null,
        }
      }
      await api.saveStrategyVersion(active.name, {
        runtime_params,
        train_params,
        label: versionLabel.trim() || 'params',
        kind: 'params',
        make_active: true,
      })
      setVersionLabel('')
      await refreshVersions(active.name)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  const loadVersion = async (versionId: string) => {
    if (!active) return
    setLoadingVersion(versionId)
    setError(null)
    try {
      const detail = await api.activateStrategyVersion(active.name, versionId)
      setRuntimeParamsText(pretty((detail.runtime_params || {}) as Record<string, unknown>))
      const tp = detail.train_params || {}
      if (typeof tp.num_boost_round === 'number') setNumBoostRound(tp.num_boost_round)
      if (typeof tp.early_stopping_rounds === 'number') setEarlyStopping(tp.early_stopping_rounds)
      if (tp.max_markets != null && tp.max_markets !== '') setMaxMarkets(String(tp.max_markets))
      else setMaxMarkets('')
      if (typeof tp.horizon_seconds === 'number') setHorizonSeconds(tp.horizon_seconds)
      if (typeof tp.delta_seconds === 'number') setDeltaSeconds(tp.delta_seconds)
      if (typeof tp.train_ratio === 'number') setTrainRatio(tp.train_ratio)
      await refreshVersions(active.name)
      if (active.name === 'lgbm_edge') await refreshModel()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoadingVersion(null)
    }
  }

  const startTrain = async () => {
    if (!active) return
    setError(null)
    setTraining(true)
    try {
      const runtime_params = parseRuntimeParams()
      if (active.name === 'momentum_pair') {
        try {
          await api.saveStrategyVersion('momentum_pair', {
            runtime_params,
            train_params: {
              horizon_seconds: horizonSeconds,
              delta_seconds: deltaSeconds,
              train_ratio: trainRatio,
              num_boost_round: numBoostRound,
              early_stopping_rounds: earlyStopping,
              max_markets: maxMarkets.trim() ? Number(maxMarkets) : null,
            },
            label: versionLabel.trim() || 'pre-train params',
            kind: 'params',
            make_active: true,
          })
        } catch {
          /* continue */
        }
        const body: {
          horizon_seconds: number
          delta_seconds: number
          train_ratio: number
          num_boost_round: number
          early_stopping_rounds: number
          max_markets?: number | null
        } = {
          horizon_seconds: horizonSeconds,
          delta_seconds: deltaSeconds,
          train_ratio: trainRatio,
          num_boost_round: numBoostRound,
          early_stopping_rounds: earlyStopping,
        }
        const mm = maxMarkets.trim()
        if (mm) body.max_markets = Number(mm)
        const res = await api.momentumPairTrain(body)
        setMpJob(res.job)
        return
      }

      try {
        await api.saveStrategyVersion('lgbm_edge', {
          runtime_params,
          train_params: {
            num_boost_round: numBoostRound,
            early_stopping_rounds: earlyStopping,
            max_markets: maxMarkets.trim() ? Number(maxMarkets) : null,
          },
          label: versionLabel.trim() || 'pre-train params',
          kind: 'params',
          make_active: true,
        })
      } catch {
        /* training can still proceed */
      }
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
  const isMomentum = active?.name === 'momentum_pair'

  return (
    <div className="strategy-page">
      <div className="strategy-hero">
        <div>
          <h1>Strategy</h1>
          <p className="muted">
            Each strategy keeps timestamped parameter / train snapshots under{' '}
            <code>data/strategy_versions/&lt;name&gt;/</code>. Save or load any previous set.
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

              <section className="panel strategy-panel">
                <div className="strategy-panel-head">
                  <h2>Saved versions</h2>
                  <button
                    type="button"
                    className="strategy-refresh"
                    onClick={() =>
                      void refreshVersions(active.name).catch((e) => setError(String(e)))
                    }
                  >
                    Refresh
                  </button>
                </div>
                <p className="muted strategy-path-line">
                  Folder: <code>{versionsDir || `data/strategy_versions/${active.name}`}</code>
                  {activeVersionId ? ` · active ${activeVersionId}` : ' · no active version'}
                </p>

                <div className="strategy-section">
                  <h3>Edit runtime parameters</h3>
                  <textarea
                    className="strategy-params-editor"
                    rows={12}
                    value={runtimeParamsText}
                    onChange={(e) => setRuntimeParamsText(e.target.value)}
                    spellCheck={false}
                  />
                  <div className="strategy-version-actions">
                    <label>
                      Label (optional)
                      <input
                        type="text"
                        value={versionLabel}
                        placeholder="e.g. tighter threshold"
                        onChange={(e) => setVersionLabel(e.target.value)}
                      />
                    </label>
                    <button
                      type="button"
                      className="strategy-train-btn"
                      disabled={saving}
                      onClick={() => void saveParamsVersion()}
                    >
                      {saving ? 'Saving…' : 'Save parameter set'}
                    </button>
                  </div>
                </div>

                <div className="strategy-section">
                  <h3>Previous versions</h3>
                  {versions.length === 0 ? (
                    <p className="muted strategy-empty">
                      No saved files yet. Save parameters
                      {active.trainable ? ' or run training' : ''} to create a timestamped snapshot.
                    </p>
                  ) : (
                    <ul className="strategy-version-list">
                      {versions.map((v) => (
                        <li key={v.id} className={v.active ? 'active' : ''}>
                          <div className="strategy-version-meta">
                            <strong>
                              <code>{v.id}</code>
                              {v.active ? <span className="strategy-pill">active</span> : null}
                              {v.kind === 'train' ? (
                                <span className="strategy-pill strategy-pill-train">train</span>
                              ) : null}
                              {v.has_model ? (
                                <span className="strategy-pill strategy-pill-model">model</span>
                              ) : null}
                            </strong>
                            <span className="muted">
                              {v.created_at || '—'}
                              {v.label ? ` · ${v.label}` : ''}
                            </span>
                            {v.metrics_summary &&
                              typeof v.metrics_summary === 'object' &&
                              (v.metrics_summary as { validation?: { auc?: number } }).validation
                                ?.auc != null && (
                                <span className="muted">
                                  valid AUC{' '}
                                  {fmtNum(
                                    (v.metrics_summary as { validation?: { auc?: number } })
                                      .validation?.auc,
                                    4,
                                  )}
                                </span>
                              )}
                          </div>
                          <button
                            type="button"
                            className="strategy-refresh"
                            disabled={loadingVersion === v.id || v.active}
                            onClick={() => void loadVersion(v.id)}
                          >
                            {loadingVersion === v.id ? 'Loading…' : v.active ? 'Loaded' : 'Load'}
                          </button>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
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
                    <p className="muted strategy-path-line">
                      Successful trains auto-save a timestamped version (model + metrics + params).
                    </p>
                    {job && (
                      <div className={`strategy-job strategy-job-${job.status}`}>
                        <div>
                          Status: <strong>{job.status}</strong>
                          {job.pid != null ? ` · pid ${job.pid}` : ''}
                        </div>
                        {job.started_at && <div className="muted">Started {job.started_at}</div>}
                        {job.finished_at && <div className="muted">Finished {job.finished_at}</div>}
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

              {isMomentum && (
                <section className="panel strategy-panel">
                  <div className="strategy-panel-head">
                    <h2>Train UP mid predictor</h2>
                    <button
                      type="button"
                      className="strategy-refresh"
                      onClick={() =>
                        void api
                          .momentumPairTrainStatus()
                          .then(setMpJob)
                          .catch((e) => setError(String(e)))
                      }
                    >
                      Refresh status
                    </button>
                  </div>

                  <div className="strategy-section">
                    <h3>Split</h3>
                    <p className="muted strategy-path-line">
                      Data: <code>E:\DataSets\poly\live</code> (fetch_live VWAP). Chronological{' '}
                      <strong>80% train / 20% test</strong> by market (no shuffle). Early stopping
                      uses the last 15% of the train slice.
                    </p>
                  </div>

                  <div className="strategy-section">
                    <h3>Train parameters</h3>
                    <div className="strategy-train-form">
                      <label>
                        horizon T (seconds)
                        <input
                          type="number"
                          min={1}
                          max={60}
                          value={horizonSeconds}
                          onChange={(e) => setHorizonSeconds(Number(e.target.value) || 5)}
                        />
                      </label>
                      <label>
                        delta (seconds)
                        <input
                          type="number"
                          min={1}
                          max={30}
                          value={deltaSeconds}
                          onChange={(e) => setDeltaSeconds(Number(e.target.value) || 1)}
                        />
                      </label>
                      <label>
                        train_ratio
                        <input
                          type="number"
                          min={0.5}
                          max={0.95}
                          step={0.05}
                          value={trainRatio}
                          onChange={(e) => setTrainRatio(Number(e.target.value) || 0.8)}
                        />
                      </label>
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
                        disabled={training || mpJob?.status === 'running'}
                        onClick={() => void startTrain()}
                      >
                        {mpJob?.status === 'running' || training ? 'Training…' : 'Start training'}
                      </button>
                    </div>
                    <p className="muted strategy-path-line">
                      Successful trains auto-save a timestamped version (model + metrics + params).
                    </p>
                    {mpJob && (
                      <div className={`strategy-job strategy-job-${mpJob.status}`}>
                        <div>
                          Status: <strong>{mpJob.status}</strong>
                          {mpJob.phase ? ` · ${mpJob.phase}` : ''}
                        </div>
                        {(mpJob.status === 'running' ||
                          (typeof mpJob.progress === 'number' && mpJob.progress > 0)) && (
                          <div className="strategy-progress">
                            <div className="strategy-progress-track">
                              <div
                                className="strategy-progress-fill"
                                style={{
                                  width: `${Math.max(0, Math.min(100, Number(mpJob.progress) || 0))}%`,
                                }}
                              />
                            </div>
                            <div className="strategy-progress-label">
                              {Math.round(Number(mpJob.progress) || 0)}%
                              {mpJob.message ? ` · ${mpJob.message}` : ''}
                            </div>
                          </div>
                        )}
                        {mpJob.started_at && (
                          <div className="muted">Started {mpJob.started_at}</div>
                        )}
                        {mpJob.finished_at && (
                          <div className="muted">Finished {mpJob.finished_at}</div>
                        )}
                        {mpJob.error && <div className="strategy-error">{mpJob.error}</div>}
                        {mpJob.metrics && (
                          <div className="strategy-section" style={{ marginTop: '0.5rem' }}>
                            <MetricsBlock metrics={mpJob.metrics} />
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                </section>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  )
}
