import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api'

type PmMissingRow = {
  market_id: string
  slug?: string | null
  date_et?: string | null
  time_et?: string | null
  start_time: number
  end_time: number
}

const GEN_CONCURRENCY = 32

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

type QueueKind = 'books' | 'chainlink'

function PmFetchPanel({
  kind,
  title,
  note,
  ariaList,
}: {
  kind: QueueKind
  title: string
  note: string
  ariaList: string
}) {
  const [missing, setMissing] = useState<PmMissingRow[]>([])
  const [stats, setStats] = useState({ total: 0, present: 0, missing: 0 })
  const [loading, setLoading] = useState(false)
  const [generating, setGenerating] = useState(false)
  const [genIds, setGenIds] = useState<Set<string>>(() => new Set())
  const [progress, setProgress] = useState({ done: 0, total: 0 })
  const [message, setMessage] = useState<string | null>(null)
  const [messageTone, setMessageTone] = useState<'info' | 'ok' | 'err'>('info')
  const abort = useRef(false)
  const activeRowRef = useRef<HTMLDivElement | null>(null)

  const coveragePct = stats.total > 0 ? Math.round((stats.present / stats.total) * 100) : 0
  const groups = useMemo(() => groupByDate(missing), [missing])

  const refresh = async () => {
    setLoading(true)
    try {
      const res =
        kind === 'books' ? await api.missingPmOrderbooks() : await api.missingPmChainlink()
      setMissing(res.missing || [])
      setStats({
        total: res.n_total || 0,
        present: res.n_present || 0,
        missing: res.n_missing || 0,
      })
    } catch (err) {
      setMessageTone('err')
      setMessage(
        err instanceof Error
          ? err.message
          : kind === 'books'
            ? 'Failed to list missing PM books'
            : 'Failed to list missing PM chainlink',
      )
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void refresh()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kind])

  useEffect(() => {
    if (!genIds.size) return
    activeRowRef.current?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
  }, [genIds])

  const markActive = (mid: string, on: boolean) => {
    setGenIds((prev) => {
      const next = new Set(prev)
      if (on) next.add(mid)
      else next.delete(mid)
      return next
    })
  }

  const generateOne = async (mid: string) => {
    markActive(mid, true)
    setMessage(null)
    setMessageTone('info')
    try {
      const res =
        kind === 'books'
          ? await api.generatePmOrderbooks(mid)
          : await api.generatePmChainlink(mid)
      setMissing((prev) => prev.filter((m) => m.market_id !== mid))
      setStats((s) => ({
        total: s.total,
        present: s.present + 1,
        missing: Math.max(0, s.missing - 1),
      }))
      const warn = res.warning ? ` · ${res.warning}` : ''
      setMessageTone(res.warning ? 'info' : 'ok')
      setMessage(`${mid} · ${res.n_rows ?? 0} rows${warn}`)
    } catch (err) {
      setMessageTone('err')
      setMessage(err instanceof Error ? err.message : `Generate failed for ${mid}`)
      throw err
    } finally {
      markActive(mid, false)
    }
  }

  const generateAll = async () => {
    if (generating || !missing.length) return
    abort.current = false
    setGenerating(true)
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
        const mid = queue[i].market_id
        try {
          await generateOne(mid)
          done += 1
        } catch (err) {
          failed += 1
          lastErr = err instanceof Error ? err.message : String(err)
        }
        setProgress({ done: done + failed, total: queue.length })
      }
    }

    const nWorkers = Math.min(GEN_CONCURRENCY, queue.length)
    await Promise.all(Array.from({ length: nWorkers }, () => worker()))

    setGenerating(false)
    setGenIds(new Set())
    const stopped = abort.current
    setMessageTone(failed ? 'err' : stopped ? 'info' : 'ok')
    const summary = stopped
      ? `Stopped · ${done} generated${failed ? `, ${failed} failed` : ''} · ${nWorkers}× parallel`
      : `Complete · ${done} generated${failed ? `, ${failed} failed` : ''} · ${nWorkers}× parallel`
    setMessage(lastErr ? `${summary}\n${lastErr}` : summary)
    void refresh()
  }

  return (
    <section className="sidebar-section pm-books-section pmdata-panel">
      <div className="sidebar-heading data-heading">
        <span>{title}</span>
        <span
          className={`pm-books-pill${
            loading
              ? ''
              : stats.missing === 0 && stats.total > 0
                ? ' ok'
                : stats.missing > 0
                  ? ' pending'
                  : ''
          }`}
        >
          {loading ? '…' : `${coveragePct}%`}
        </span>
      </div>

      <div
        className="pm-books-meter"
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={coveragePct}
        aria-label={`${title} coverage`}
      >
        <div className="pm-books-meter-fill" style={{ width: `${coveragePct}%` }} />
      </div>

      <div className="pm-books-stats">
        <span>
          <strong>{stats.present.toLocaleString()}</strong> ready
        </span>
        <span className="pm-books-stats-sep" aria-hidden>
          ·
        </span>
        <span>
          <strong>{stats.missing.toLocaleString()}</strong> queued
        </span>
        <span className="pm-books-stats-note">{note}</span>
      </div>

      <div className="pm-books-actions">
        {generating ? (
          <button
            type="button"
            className="sidebar-btn pm-books-stop"
            onClick={() => {
              abort.current = true
            }}
          >
            Stop
          </button>
        ) : (
          <button
            type="button"
            className="sidebar-btn primary pm-books-gen"
            disabled={loading || missing.length === 0}
            onClick={() => void generateAll()}
            title={`Generate for every missing past market, oldest first`}
          >
            Generate queue
          </button>
        )}
        <button
          type="button"
          className="sidebar-btn ghost pm-books-refresh"
          disabled={loading || generating}
          onClick={() => void refresh()}
          title="Refresh missing list"
          aria-label={`Refresh ${title}`}
        >
          <svg viewBox="0 0 24 24" width="14" height="14" aria-hidden>
            <path
              fill="currentColor"
              d="M17.65 6.35A7.95 7.95 0 0 0 12 4V1L7 6l5 5V7c2.76 0 5 2.24 5 5a5 5 0 0 1-8.66 3.54l-1.42 1.42A7 7 0 0 0 19 12c0-1.93-.78-3.68-2.05-4.95zM6 12c0-1.07.34-2.07.92-2.89l-1.43-1.43A6.97 6.97 0 0 0 5 12a7 7 0 0 0 11.89 5.04l-1.42-1.42A5 5 0 0 1 6 12z"
            />
          </svg>
        </button>
      </div>

      {generating && progress.total > 0 && (
        <div className="pm-books-run">
          <div className="pm-books-run-head">
            <span className="pm-books-run-label">Generating · {GEN_CONCURRENCY}×</span>
            <span className="pm-books-run-count">
              {progress.done}/{progress.total}
            </span>
          </div>
          <div className="pm-books-run-bar" aria-hidden>
            <div
              className="pm-books-run-fill"
              style={{
                width: `${Math.min(100, Math.round((progress.done / progress.total) * 100))}%`,
              }}
            />
          </div>
          {genIds.size > 0 && (
            <div className="pm-books-run-id">
              {genIds.size} active · {[...genIds].slice(0, 3).join(', ')}
              {genIds.size > 3 ? '…' : ''}
            </div>
          )}
        </div>
      )}

      {message && <p className={`pm-books-msg ${messageTone}`}>{message}</p>}

      <div
        className={`pm-books-list${!loading && !missing.length ? ' disabled' : ''}`}
        role="list"
        aria-label={ariaList}
      >
        {loading ? (
          <div className="time-window-empty">Scanning history…</div>
        ) : missing.length === 0 ? (
          <div className="time-window-empty pm-books-empty-ok">All past markets covered</div>
        ) : (
          groups.map(([date, rows]) => (
            <div key={date} className="pm-books-group">
              <div className="pm-books-group-head">
                <span>{date}</span>
                <span>{rows.length}</span>
              </div>
              {rows.map((m) => {
                const active = genIds.has(m.market_id)
                return (
                  <div
                    key={m.market_id}
                    ref={
                      active
                        ? (el) => {
                            if (el) activeRowRef.current = el
                          }
                        : undefined
                    }
                    className={`pm-books-row${active ? ' active' : ''}`}
                    role="listitem"
                  >
                    <div className="pm-books-slot" title={m.slug || m.market_id}>
                      <span className="pm-books-time">
                        {formatSlotLabel(m.time_et || '', m.start_time, m.end_time)}
                      </span>
                      <span className="pm-books-id">{m.market_id}</span>
                    </div>
                    <button
                      type="button"
                      className="pm-books-one"
                      disabled={generating}
                      onClick={() => void generateOne(m.market_id).catch(() => undefined)}
                      title={`Generate ${m.market_id}`}
                    >
                      {active ? (
                        <span className="pm-books-spinner" aria-label="Generating" />
                      ) : (
                        'Run'
                      )}
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
      <header className="pmdata-hero">
        <h1>PMData</h1>
        <p>
          Fetch past-market order books and Chainlink prices from PMData into{' '}
          <code>pm_orderbooks.parquet</code> / <code>pm_chainlink_price.parquet</code>. Oldest
          first · live window excluded.
        </p>
      </header>

      <div className="pmdata-grid">
        <PmFetchPanel
          kind="books"
          title="PM books"
          note="Oldest first"
          ariaList="Past markets missing pm_orderbooks"
        />
        <PmFetchPanel
          kind="chainlink"
          title="PM chainlink"
          note="0.5s · Oldest first"
          ariaList="Past markets missing pm_chainlink_price"
        />
      </div>
    </div>
  )
}
