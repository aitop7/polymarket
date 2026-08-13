import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api'

type PmMissingRow = {
  market_id: string
  slug?: string | null
  date_et?: string | null
  time_et?: string | null
  start_time: number
  end_time: number
  reasons?: string[]
  data_health?: string
}

type QueueKind = 'books' | 'chainlink' | 'health'

const CONCURRENCY: Record<QueueKind, number> = {
  books: 32,
  chainlink: 32,
  health: 16,
}

function formatSlotLabel(timeEt: string, startMs?: number, endMs?: number): string {
  if (startMs != null && endMs != null) {
    const opts: Intl.DateTimeFormatOptions = {
      timeZone: 'America/New_York',
      hour: 'numeric',
      minute: '2-digit',
      hour12: true,
    }
    const a = new Intl.DateTimeFormat('en-US', opts).format(new Date(startMs))
    const b = new Intl.DateTimeFormat('en-US', opts).format(new Date(endMs))
    return `${a}–${b}`
  }
  return timeEt || '—'
}

function groupByDate<T extends { date_et?: string | null }>(rows: T[]): [string, T[]][] {
  const map = new Map<string, T[]>()
  for (const m of rows) {
    const key = m.date_et || 'Unknown'
    const list = map.get(key)
    if (list) list.push(m)
    else map.set(key, [m])
  }
  return [...map.entries()]
}

function RefreshIcon() {
  return (
    <svg viewBox="0 0 24 24" width="13" height="13" aria-hidden>
      <path
        fill="currentColor"
        d="M17.65 6.35A7.95 7.95 0 0 0 12 4V1L7 6l5 5V7c2.76 0 5 2.24 5 5a5 5 0 0 1-8.66 3.54l-1.42 1.42A7 7 0 0 0 19 12c0-1.93-.78-3.68-2.05-4.95zM6 12c0-1.07.34-2.07.92-2.89l-1.43-1.43A6.97 6.97 0 0 0 5 12a7 7 0 0 0 11.89 5.04l-1.42-1.42A5 5 0 0 1 6 12z"
      />
    </svg>
  )
}

function PmQueuePanel({
  kind,
  title,
  actionLabel,
  readyLabel,
  queuedLabel,
  emptyOk,
  emptyNone,
  ariaList,
}: {
  kind: QueueKind
  title: string
  actionLabel: string
  readyLabel: string
  queuedLabel: string
  emptyOk: string
  emptyNone: string
  ariaList: string
}) {
  const [missing, setMissing] = useState<PmMissingRow[]>([])
  const [stats, setStats] = useState({ total: 0, present: 0, missing: 0 })
  const [loading, setLoading] = useState(false)
  const [running, setRunning] = useState(false)
  const [activeIds, setActiveIds] = useState<Set<string>>(() => new Set())
  const [progress, setProgress] = useState({ done: 0, total: 0 })
  const [message, setMessage] = useState<string | null>(null)
  const [messageTone, setMessageTone] = useState<'info' | 'ok' | 'err'>('info')
  const abort = useRef(false)
  const activeRowRef = useRef<HTMLDivElement | null>(null)
  const concurrency = CONCURRENCY[kind]

  const coveragePct = stats.total > 0 ? Math.round((stats.present / stats.total) * 100) : 0
  const groups = useMemo(() => groupByDate(missing), [missing])
  const pillClass =
    loading ? '' : stats.missing === 0 && stats.total > 0 ? ' ok' : stats.missing > 0 ? ' pending' : ''

  const refresh = async () => {
    setLoading(true)
    try {
      const res =
        kind === 'books'
          ? await api.missingPmOrderbooks()
          : kind === 'chainlink'
            ? await api.missingPmChainlink()
            : await api.missingPmHealthRescore()
      setMissing(res.missing || [])
      setStats({
        total: res.n_total || 0,
        present: res.n_present || 0,
        missing: res.n_missing || 0,
      })
    } catch (err) {
      setMessageTone('err')
      setMessage(err instanceof Error ? err.message : `Failed to load ${title}`)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void refresh()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kind])

  useEffect(() => {
    if (!activeIds.size) return
    activeRowRef.current?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
  }, [activeIds])

  const markActive = (mid: string, on: boolean) => {
    setActiveIds((prev) => {
      const next = new Set(prev)
      if (on) next.add(mid)
      else next.delete(mid)
      return next
    })
  }

  const runOne = async (mid: string) => {
    markActive(mid, true)
    setMessage(null)
    setMessageTone('info')
    try {
      if (kind === 'health') {
        const res = await api.rescorePmdataHealth(mid)
        const src = [res.orderbooks_source, res.chainlink_source].filter(Boolean).join(' · ')
        setMessageTone('ok')
        setMessage(`${mid} · ${res.data_health}${src ? ` · ${src}` : ''}`)
      } else {
        const res =
          kind === 'books'
            ? await api.generatePmOrderbooks(mid)
            : await api.generatePmChainlink(mid)
        const warn = res.warning ? ` · ${res.warning}` : ''
        setMessageTone(res.warning ? 'info' : 'ok')
        setMessage(`${mid} · ${res.n_rows ?? 0} rows${warn}`)
      }
      setMissing((prev) => prev.filter((m) => m.market_id !== mid))
      setStats((s) => ({
        total: s.total,
        present: s.present + 1,
        missing: Math.max(0, s.missing - 1),
      }))
    } catch (err) {
      setMessageTone('err')
      setMessage(err instanceof Error ? err.message : `Failed ${mid}`)
      throw err
    } finally {
      markActive(mid, false)
    }
  }

  const runAll = async () => {
    if (running || !missing.length) return
    abort.current = false
    setRunning(true)
    setMessage(null)
    setMessageTone('info')
    const queue = [...missing].sort(
      (a, b) => (a.start_time || 0) - (b.start_time || 0) || a.market_id.localeCompare(b.market_id),
    )
    setProgress({ done: 0, total: queue.length })
    let cursor = 0
    let done = 0
    let failed = 0
    let lastErr: string | null = null

    const worker = async () => {
      while (!abort.current) {
        const i = cursor++
        if (i >= queue.length) return
        try {
          await runOne(queue[i].market_id)
          done += 1
        } catch (err) {
          failed += 1
          lastErr = err instanceof Error ? err.message : String(err)
        }
        setProgress({ done: done + failed, total: queue.length })
      }
    }

    const nWorkers = Math.min(concurrency, queue.length)
    await Promise.all(Array.from({ length: nWorkers }, () => worker()))

    setRunning(false)
    setActiveIds(new Set())
    const stopped = abort.current
    setMessageTone(failed ? 'err' : stopped ? 'info' : 'ok')
    const verb = kind === 'health' ? 'rescored' : 'generated'
    const summary = stopped
      ? `Stopped · ${done} ${verb}${failed ? `, ${failed} failed` : ''}`
      : `Done · ${done} ${verb}${failed ? `, ${failed} failed` : ''}`
    setMessage(lastErr ? `${summary}\n${lastErr}` : summary)
    void refresh()
  }

  return (
    <section className="pmq">
      <header className="pmq-head">
        <div className="pmq-title">
          <h2>{title}</h2>
          <span className={`pmq-pill${pillClass}`}>{loading ? '…' : `${coveragePct}%`}</span>
        </div>
        <div className="pmq-actions">
          {running ? (
            <button type="button" className="pmq-btn stop" onClick={() => { abort.current = true }}>
              Stop
            </button>
          ) : (
            <button
              type="button"
              className="pmq-btn primary"
              disabled={loading || missing.length === 0}
              onClick={() => void runAll()}
            >
              {actionLabel}
            </button>
          )}
          <button
            type="button"
            className="pmq-btn icon"
            disabled={loading || running}
            onClick={() => void refresh()}
            title={`Refresh ${title}`}
            aria-label={`Refresh ${title}`}
          >
            <RefreshIcon />
          </button>
        </div>
      </header>

      <div
        className="pmq-meter"
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={coveragePct}
        aria-label={`${title} coverage`}
      >
        <div className="pmq-meter-fill" style={{ width: `${coveragePct}%` }} />
      </div>

      <div className="pmq-meta">
        <span>
          <strong>{stats.present.toLocaleString()}</strong> {readyLabel}
        </span>
        <span className="pmq-dot" aria-hidden>
          ·
        </span>
        <span>
          <strong>{stats.missing.toLocaleString()}</strong> {queuedLabel}
        </span>
        {running && progress.total > 0 ? (
          <>
            <span className="pmq-dot" aria-hidden>
              ·
            </span>
            <span className="pmq-run">
              {progress.done}/{progress.total}
              {activeIds.size > 0 ? ` · ${activeIds.size}×` : ''}
            </span>
          </>
        ) : null}
      </div>

      {message ? <p className={`pmq-msg ${messageTone}`}>{message}</p> : null}

      <div className={`pmq-list${!loading && !missing.length ? ' empty' : ''}`} aria-label={ariaList}>
        {loading ? (
          <div className="pmq-empty">Loading…</div>
        ) : missing.length === 0 ? (
          <div className="pmq-empty ok">{stats.total > 0 ? emptyOk : emptyNone}</div>
        ) : (
          groups.map(([date, rows]) => (
            <div key={date} className="pmq-group">
              <div className="pmq-group-head">
                <span>{date}</span>
                <span>{rows.length}</span>
              </div>
              {rows.map((m) => {
                const active = activeIds.has(m.market_id)
                return (
                  <div
                    key={m.market_id}
                    className={`pmq-row${active ? ' active' : ''}`}
                    ref={active ? activeRowRef : undefined}
                  >
                    <div className="pmq-row-main" title={m.slug || m.market_id}>
                      <span className="pmq-time">
                        {formatSlotLabel(m.time_et || '', m.start_time, m.end_time)}
                      </span>
                      <span className="pmq-id">{m.market_id}</span>
                      {m.reasons?.length ? (
                        <span className="pmq-tag">{m.reasons.join('+')}</span>
                      ) : null}
                    </div>
                    <button
                      type="button"
                      className="pmq-run-one"
                      disabled={running}
                      onClick={() => void runOne(m.market_id).catch(() => undefined)}
                      title={`${actionLabel} ${m.market_id}`}
                    >
                      {active ? <span className="pmq-spinner" aria-label="Running" /> : 'Run'}
                    </button>
                  </div>
                )
              })}
            </div>
          ))
        )}
      </div>
    </section>
  )
}

type BinanceHealthRow = {
  market_id: string
  slug?: string | null
  date_et?: string | null
  time_et?: string | null
  start_time: number
  end_time: number
  grade: string
  price_grade?: string
  trade_grade?: string
  max_gap_ms?: number
  max_trade_quiet_ms?: number
  has_price?: boolean
  has_trades?: boolean
}

/** Great + good are healthy; Issues view hides them by default. */
function isBinanceHealthy(grade?: string | null): boolean {
  const g = (grade || '').toLowerCase()
  return g === 'great' || g === 'good'
}

function formatQuietMs(ms?: number): string {
  const n = Number(ms)
  if (!Number.isFinite(n) || n <= 0) return '—'
  if (n < 1000) return `${Math.round(n)}ms`
  if (n < 60_000) return `${(n / 1000).toFixed(n < 10_000 ? 1 : 0)}s`
  return `${(n / 60_000).toFixed(1)}m`
}

function BinanceHealthPanel() {
  const [rows, setRows] = useState<BinanceHealthRow[]>([])
  const [loading, setLoading] = useState(false)
  const [running, setRunning] = useState(false)
  const [issuesOnly, setIssuesOnly] = useState(true)
  const [message, setMessage] = useState<string | null>(null)
  const [messageTone, setMessageTone] = useState<'info' | 'ok' | 'err'>('info')
  const [activeIds, setActiveIds] = useState<Set<string>>(() => new Set())
  const [progress, setProgress] = useState({ done: 0, total: 0 })
  const abort = useRef(false)
  const activeRowRef = useRef<HTMLDivElement | null>(null)
  const concurrency = 32

  const { stats, counts } = useMemo(() => {
    const nextCounts: Record<string, number> = {
      great: 0,
      good: 0,
      ok: 0,
      low: 0,
      bad: 0,
      unchecked: 0,
    }
    for (const r of rows) {
      const g = (r.grade || 'unchecked').toLowerCase()
      nextCounts[g] = (nextCounts[g] || 0) + 1
    }
    const healthy = (nextCounts.great || 0) + (nextCounts.good || 0)
    return {
      counts: nextCounts,
      stats: {
        total: rows.length,
        great: nextCounts.great || 0,
        healthy,
        issues: Math.max(0, rows.length - healthy),
      },
    }
  }, [rows])

  const coveragePct = stats.total > 0 ? Math.round((stats.healthy / stats.total) * 100) : 0
  const issueRows = useMemo(() => rows.filter((r) => !isBinanceHealthy(r.grade)), [rows])
  const visible = useMemo(
    () => (issuesOnly ? issueRows : rows),
    [rows, issueRows, issuesOnly],
  )
  const groups = useMemo(() => groupByDate(visible), [visible])
  const pillClass =
    loading ? '' : stats.issues === 0 && stats.total > 0 ? ' ok' : stats.issues > 0 ? ' pending' : ''

  const refresh = async () => {
    setLoading(true)
    setMessage(null)
    try {
      const res = await api.binanceHealth()
      setRows(res.markets || [])
    } catch (err) {
      setMessageTone('err')
      setMessage(err instanceof Error ? err.message : 'Failed to load Binance health')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void refresh()
  }, [])

  useEffect(() => {
    if (!activeIds.size) return
    activeRowRef.current?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
  }, [activeIds])

  const markActive = (mid: string, on: boolean) => {
    setActiveIds((prev) => {
      const next = new Set(prev)
      if (on) next.add(mid)
      else next.delete(mid)
      return next
    })
  }

  const applyRepairResult = (mid: string, res: Awaited<ReturnType<typeof api.repairBinance>>) => {
    setRows((prev) =>
      prev.map((row) => {
        if (row.market_id !== mid) return row
        return {
          ...row,
          grade: res.grade || row.grade,
          price_grade: res.price_grade ?? row.price_grade,
          trade_grade: res.trade_grade ?? row.trade_grade,
          max_gap_ms: res.max_gap_ms ?? row.max_gap_ms,
          max_trade_quiet_ms: res.max_trade_quiet_ms ?? row.max_trade_quiet_ms,
          has_price: res.has_price ?? row.has_price,
          has_trades: res.has_trades ?? row.has_trades,
        }
      }),
    )
  }

  const runOne = async (mid: string) => {
    markActive(mid, true)
    setMessage(null)
    setMessageTone('info')
    try {
      const res = await api.repairBinance(mid)
      applyRepairResult(mid, res)
      const filled = res.filled || {}
      const added = Object.values(filled).reduce((a, b) => a + (Number(b) || 0), 0)
      setMessageTone('ok')
      setMessage(`${mid} · ${res.grade || '—'} · +${added} rows`)
    } catch (err) {
      setMessageTone('err')
      setMessage(err instanceof Error ? err.message : `Failed ${mid}`)
      throw err
    } finally {
      markActive(mid, false)
    }
  }

  const runAll = async () => {
    if (running || !issueRows.length) return
    abort.current = false
    setRunning(true)
    setMessage(null)
    setMessageTone('info')
    const queue = [...issueRows].sort(
      (a, b) => (a.start_time || 0) - (b.start_time || 0) || a.market_id.localeCompare(b.market_id),
    )
    setProgress({ done: 0, total: queue.length })
    let cursor = 0
    let done = 0
    let failed = 0
    let lastErr: string | null = null

    const worker = async () => {
      while (!abort.current) {
        const i = cursor++
        if (i >= queue.length) return
        try {
          await runOne(queue[i].market_id)
          done += 1
        } catch (err) {
          failed += 1
          lastErr = err instanceof Error ? err.message : String(err)
        }
        setProgress({ done: done + failed, total: queue.length })
      }
    }

    const nWorkers = Math.min(concurrency, queue.length)
    await Promise.all(Array.from({ length: nWorkers }, () => worker()))

    setRunning(false)
    setActiveIds(new Set())
    const stopped = abort.current
    setMessageTone(failed ? 'err' : stopped ? 'info' : 'ok')
    const summary = stopped
      ? `Stopped · ${done} fixed${failed ? `, ${failed} failed` : ''}`
      : `Done · ${done} fixed${failed ? `, ${failed} failed` : ''}`
    setMessage(lastErr ? `${summary}\n${lastErr}` : summary)
    void refresh()
  }

  return (
    <section className="pmq">
      <header className="pmq-head">
        <div className="pmq-title">
          <h2>Binance</h2>
          <span className={`pmq-pill${pillClass}`}>{loading ? '…' : `${coveragePct}%`}</span>
        </div>
        <div className="pmq-actions">
          {running ? (
            <button type="button" className="pmq-btn stop" onClick={() => { abort.current = true }}>
              Stop
            </button>
          ) : (
            <button
              type="button"
              className="pmq-btn primary"
              disabled={loading || issueRows.length === 0}
              onClick={() => void runAll()}
              title="Fetch missing Binance price/trades via Binance API"
            >
              Fix
            </button>
          )}
          <button
            type="button"
            className="pmq-btn"
            disabled={loading || running}
            onClick={() => setIssuesOnly((v) => !v)}
            title={
              issuesOnly
                ? 'Hiding great/good — click for all'
                : 'Showing all — click to hide great/good'
            }
          >
            {issuesOnly ? 'Issues' : 'All'}
          </button>
          <button
            type="button"
            className="pmq-btn icon"
            disabled={loading || running}
            onClick={() => void refresh()}
            title="Refresh Binance health"
            aria-label="Refresh Binance health"
          >
            <RefreshIcon />
          </button>
        </div>
      </header>

      <div
        className="pmq-meter"
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={coveragePct}
        aria-label="Binance healthy coverage"
      >
        <div className="pmq-meter-fill" style={{ width: `${coveragePct}%` }} />
      </div>

      <div className="pmq-meta">
        <span>
          <strong>{stats.healthy.toLocaleString()}</strong> healthy
        </span>
        <span className="pmq-dot" aria-hidden>
          ·
        </span>
        <span>
          <strong>{stats.issues.toLocaleString()}</strong> issues
        </span>
        {running && progress.total > 0 ? (
          <>
            <span className="pmq-dot" aria-hidden>
              ·
            </span>
            <span className="pmq-run">
              {progress.done}/{progress.total}
              {activeIds.size > 0 ? ` · ${activeIds.size}×` : ''}
            </span>
          </>
        ) : !loading && stats.total > 0 ? (
          <>
            <span className="pmq-dot" aria-hidden>
              ·
            </span>
            <span className="pmq-binance-counts" title="Grade counts (great+good = healthy)">
              {[
                counts.great ? `gt${counts.great}` : null,
                counts.good ? `g${counts.good}` : null,
                counts.ok ? `o${counts.ok}` : null,
                counts.low ? `l${counts.low}` : null,
                counts.bad ? `b${counts.bad}` : null,
              ]
                .filter(Boolean)
                .join(' · ') || '—'}
            </span>
          </>
        ) : null}
      </div>

      {message ? <p className={`pmq-msg ${messageTone}`}>{message}</p> : null}

      <div
        className={`pmq-list${!loading && !visible.length ? ' empty' : ''}`}
        aria-label="Binance data health by market"
      >
        {loading ? (
          <div className="pmq-empty">Scoring Binance files…</div>
        ) : visible.length === 0 ? (
          <div className="pmq-empty ok">
            {stats.total > 0
              ? issuesOnly
                ? 'No issues (great/good hidden)'
                : 'No markets'
              : 'No markets'}
          </div>
        ) : (
          groups.map(([date, dayRows]) => (
            <div key={date} className="pmq-group">
              <div className="pmq-group-head">
                <span>{date}</span>
                <span>{dayRows.length}</span>
              </div>
              {dayRows.map((m) => {
                const grade = (m.grade || 'unchecked').toLowerCase()
                const active = activeIds.has(m.market_id)
                const canFix = !isBinanceHealthy(grade)
                const detail = [
                  m.has_price === false ? 'no px' : null,
                  m.has_trades === false ? 'no tr' : null,
                  m.has_price !== false ? `px ${formatQuietMs(m.max_gap_ms)}` : null,
                  m.has_trades !== false ? `tr ${formatQuietMs(m.max_trade_quiet_ms)}` : null,
                ]
                  .filter(Boolean)
                  .join(' · ')
                return (
                  <div
                    key={m.market_id}
                    className={`pmq-row${active ? ' active' : ''}`}
                    ref={active ? activeRowRef : undefined}
                  >
                    <div className="pmq-row-main" title={detail || m.market_id}>
                      <span className="pmq-time">
                        {formatSlotLabel(m.time_et || '', m.start_time, m.end_time)}
                      </span>
                      <span className="pmq-id">{m.market_id}</span>
                      <span className={`pmq-grade health-${grade}`}>{grade}</span>
                    </div>
                    {canFix ? (
                      <button
                        type="button"
                        className="pmq-run-one"
                        disabled={running}
                        onClick={() => void runOne(m.market_id).catch(() => undefined)}
                        title={`Fix Binance data for ${m.market_id}`}
                      >
                        {active ? <span className="pmq-spinner" aria-label="Fixing" /> : 'Fix'}
                      </button>
                    ) : null}
                  </div>
                )
              })}
            </div>
          ))
        )}
      </div>
    </section>
  )
}

export default function PmDataPage() {
  return (
    <div className="pmdata-page">
      <header className="pmdata-bar">
        <div className="pmdata-bar-text">
          <h1>Data Health</h1>
          <p>
            Fill <code>pm_orderbooks</code> / <code>pm_chainlink_price</code>, then restamp health
            from those files. Binance panel grades local price/trades. Oldest first · live excluded.
          </p>
        </div>
      </header>

      <div className="pmdata-grid">
        <PmQueuePanel
          kind="books"
          title="Books"
          actionLabel="Generate"
          readyLabel="ready"
          queuedLabel="queued"
          emptyOk="All covered"
          emptyNone="No markets"
          ariaList="Past markets missing pm_orderbooks"
        />
        <PmQueuePanel
          kind="chainlink"
          title="Chainlink"
          actionLabel="Generate"
          readyLabel="ready"
          queuedLabel="queued"
          emptyOk="All covered"
          emptyNone="No markets"
          ariaList="Past markets missing pm_chainlink_price"
        />
        <PmQueuePanel
          kind="health"
          title="Health"
          actionLabel="Score PM"
          readyLabel="from PM"
          queuedLabel="to score"
          emptyOk="All scored from PM"
          emptyNone="No PM files yet"
          ariaList="Markets needing PMData health rescore"
        />
        <BinanceHealthPanel />
      </div>
    </div>
  )
}
