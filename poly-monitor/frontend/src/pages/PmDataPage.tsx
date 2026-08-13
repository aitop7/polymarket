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

function groupByDate(rows: PmMissingRow[]): [string, PmMissingRow[]][] {
  const map = new Map<string, PmMissingRow[]>()
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

export default function PmDataPage() {
  return (
    <div className="pmdata-page">
      <header className="pmdata-bar">
        <div className="pmdata-bar-text">
          <h1>PMData</h1>
          <p>
            Fill <code>pm_orderbooks</code> / <code>pm_chainlink_price</code>, then restamp health
            from those files. Oldest first · live excluded.
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
      </div>
    </div>
  )
}
