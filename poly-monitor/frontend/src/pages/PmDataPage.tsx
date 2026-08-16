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

type QueueKind = 'books' | 'chainlink'

const CONCURRENCY: Record<QueueKind, number> = {
  // PMData bans keys for bursty downloads — keep this low.
  books: 2,
  chainlink: 1,
}

function isPmDataBlockedError(message: string): boolean {
  const s = message.toLowerCase()
  return (
    s.includes('temporarily blocked') ||
    s.includes('abnormal download') ||
    s.includes('account has been temporarily blocked') ||
    s.includes('pmdata account temporarily blocked')
  )
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

function CheckIcon() {
  return (
    <svg viewBox="0 0 24 24" width="11" height="11" aria-hidden>
      <path
        fill="currentColor"
        d="M9.55 17.6 4.9 12.95l1.4-1.4 3.25 3.25 7.25-7.25 1.4 1.4z"
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
        kind === 'books' ? await api.missingPmOrderbooks() : await api.missingPmChainlink()
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
      const res =
        kind === 'books'
          ? await api.generatePmOrderbooks(mid)
          : await api.generatePmChainlink(mid)
      const warn = res.warning ? ` · ${res.warning}` : ''
      const health = res.data_health ? ` · health ${res.data_health}` : ''
      setMessageTone(res.warning ? 'info' : 'ok')
      setMessage(`${mid} · ${res.n_rows ?? 0} rows${health}${warn}`)
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
          if (lastErr && isPmDataBlockedError(lastErr)) {
            abort.current = true
          }
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
    const verb = 'generated'
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

type TapeHealthRow = {
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
  trades_repaired_complete?: boolean
  binance_price_checked?: boolean
  binance_trades_checked?: boolean
}

type TapeKind = 'trades' | 'binance-price' | 'binance-trades'

/** Great + good are healthy; Issues view hides them by default. */
function isTapeHealthy(grade?: string | null): boolean {
  const g = (grade || '').toLowerCase()
  return g === 'great' || g === 'good'
}

/** True when this panel's Fix stamp is set on meta (won't requeue). */
function isTapeChecked(kind: TapeKind, r: TapeHealthRow): boolean {
  if (kind === 'binance-price') return Boolean(r.binance_price_checked)
  if (kind === 'binance-trades') return Boolean(r.binance_trades_checked)
  return Boolean(r.trades_repaired_complete)
}

/** Filter chips — all grades except great (great is always fine / omitted).
 *  `unchecked` = not yet Fix-stamped in meta.json (not a gap grade). */
type TapeGradeFilter = 'all' | 'good' | 'ok' | 'low' | 'bad' | 'unchecked'

const TAPE_GRADE_FILTERS: TapeGradeFilter[] = [
  'all',
  'good',
  'ok',
  'low',
  'bad',
  'unchecked',
]

function formatQuietMs(ms?: number): string {
  const n = Number(ms)
  if (!Number.isFinite(n) || n <= 0) return '—'
  if (n < 1000) return `${Math.round(n)}ms`
  if (n < 60_000) return `${(n / 1000).toFixed(n < 10_000 ? 1 : 0)}s`
  return `${(n / 60_000).toFixed(1)}m`
}

function tapeMeta(kind: TapeKind): {
  title: string
  fileHint: string
  concurrency: number
} {
  if (kind === 'trades') {
    return { title: 'Trades', fileHint: 'trades.parquet', concurrency: 16 }
  }
  if (kind === 'binance-price') {
    return {
      title: 'Binance Price',
      fileHint: 'binance_price_orderbook.parquet',
      concurrency: 20,
    }
  }
  return {
    title: 'Binance Trades',
    fileHint: 'binance_trades.parquet',
    concurrency: 20,
  }
}

function TapeHealthPanel({ kind }: { kind: TapeKind }) {
  const { title, fileHint, concurrency } = tapeMeta(kind)
  const [rows, setRows] = useState<TapeHealthRow[]>([])
  const [loading, setLoading] = useState(false)
  const [running, setRunning] = useState(false)
  const [gradeFilter, setGradeFilter] = useState<TapeGradeFilter>('unchecked')
  const [message, setMessage] = useState<string | null>(null)
  const [messageTone, setMessageTone] = useState<'info' | 'ok' | 'err'>('info')
  const [activeIds, setActiveIds] = useState<Set<string>>(() => new Set())
  const [progress, setProgress] = useState({ done: 0, total: 0 })
  const abort = useRef(false)
  const activeRowRef = useRef<HTMLDivElement | null>(null)

  const issueGrade = (r: TapeHealthRow) => {
    if (kind === 'binance-price') return r.price_grade || r.grade
    if (kind === 'binance-trades') return r.trade_grade || r.grade
    return r.grade
  }

  const { stats, counts } = useMemo(() => {
    const nextCounts: Record<string, number> = {
      great: 0,
      good: 0,
      ok: 0,
      low: 0,
      bad: 0,
      unchecked: 0,
    }
    let healthy = 0
    for (const r of rows) {
      const g = (issueGrade(r) || 'unchecked').toLowerCase()
      // Gap grades (great/good/ok/low/bad) from scoring.
      if (g === 'great' || g === 'good' || g === 'ok' || g === 'low' || g === 'bad') {
        nextCounts[g] = (nextCounts[g] || 0) + 1
      }
      // `unchecked` chip = not Fix-stamped yet (meta flag missing).
      if (!isTapeChecked(kind, r)) {
        nextCounts.unchecked += 1
      }
      if (isTapeHealthy(g)) healthy += 1
    }
    return {
      counts: nextCounts,
      stats: {
        total: rows.length,
        healthy,
        issues: Math.max(0, rows.length - healthy),
      },
    }
  }, [rows, kind])

  const coveragePct = stats.total > 0 ? Math.round((stats.healthy / stats.total) * 100) : 0
  const visible = useMemo(() => {
    if (gradeFilter === 'all') return rows
    if (gradeFilter === 'unchecked') {
      return rows.filter((r) => !isTapeChecked(kind, r))
    }
    return rows.filter(
      (r) => (issueGrade(r) || '').toLowerCase() === gradeFilter,
    )
  }, [rows, gradeFilter, kind])
  /** Fix targets: current filter, or non-healthy when All. */
  const fixRows = useMemo(() => {
    if (gradeFilter === 'all') {
      return rows.filter((r) => !isTapeHealthy(issueGrade(r)))
    }
    return visible
  }, [rows, visible, gradeFilter, kind])
  const groups = useMemo(() => groupByDate(visible), [visible])
  const pillClass =
    loading ? '' : stats.issues === 0 && stats.total > 0 ? ' ok' : stats.issues > 0 ? ' pending' : ''

  const refresh = async (opts?: { quiet?: boolean }) => {
    const quiet = Boolean(opts?.quiet)
    if (!quiet) {
      setLoading(true)
      setMessage(null)
    }
    try {
      const res = kind === 'trades' ? await api.tradesHealth() : await api.binanceHealth()
      setRows(res.markets || [])
    } catch (err) {
      if (!quiet) {
        setMessageTone('err')
        setMessage(err instanceof Error ? err.message : `Failed to load ${title}`)
      }
    } finally {
      if (!quiet) setLoading(false)
    }
  }

  useEffect(() => {
    void refresh()
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

  const applyFixedRow = (mid: string, patch: Partial<TapeHealthRow>) => {
    setRows((prev) =>
      prev.map((row) => (row.market_id === mid ? { ...row, ...patch } : row)),
    )
  }

  const runOne = async (mid: string) => {
    markActive(mid, true)
    setMessage(null)
    setMessageTone('info')
    try {
      if (kind === 'binance-price' || kind === 'binance-trades') {
        const part = kind === 'binance-price' ? 'price' : 'trades'
        const res = await api.repairBinance(mid, { part })
        applyFixedRow(mid, {
          grade: res.grade || 'unchecked',
          price_grade: res.price_grade,
          trade_grade: res.trade_grade ?? res.grade,
          max_gap_ms: res.max_gap_ms,
          max_trade_quiet_ms: res.max_trade_quiet_ms,
          has_price: res.has_price,
          has_trades: res.has_trades,
          binance_price_checked: Boolean(res.binance_price_checked),
          binance_trades_checked: Boolean(res.binance_trades_checked),
        })
        const filled = res.filled || {}
        const key =
          kind === 'binance-price'
            ? 'binance_price_orderbook.parquet'
            : 'binance_trades.parquet'
        const added = Number(filled[key] ?? Object.values(filled)[0] ?? 0) || 0
        const grade =
          (kind === 'binance-price' ? res.price_grade : res.trade_grade) ||
          res.grade ||
          '—'
        setMessageTone('ok')
        setMessage(
          `${mid} · ${grade} · +${added} rows${
            res.data_health ? ` · health ${res.data_health}` : ''
          }`,
        )
        // Local row patch is enough — skip full /binance-health rescore (slow).
      } else {
        // Local Data API only — never pulls from VPS.
        const res = await api.repairTradesLocal(mid)
        if (!res.ok) throw new Error(res.error || `Failed ${mid}`)
        applyFixedRow(mid, {
          grade: res.grade || 'unchecked',
          trade_grade: res.trade_grade ?? res.grade,
          max_trade_quiet_ms: res.max_trade_quiet_ms,
          has_trades: res.has_trades ?? true,
          trades_repaired_complete: Boolean(res.trades_repaired_complete),
        })
        const added = res.trade_rows_added ?? 0
        const grade = res.trade_grade || res.grade || '—'
        setMessageTone('ok')
        setMessage(
          `${mid} · ${grade} · +${added} trades (local)${
            res.data_health ? ` · health ${res.data_health}` : ''
          }`,
        )
        // Reload grades so Issues list drops fixed markets right away.
        if (!running) {
          await refresh({ quiet: true })
        }
      }
    } catch (err) {
      setMessageTone('err')
      setMessage(err instanceof Error ? err.message : `Failed ${mid}`)
      throw err
    } finally {
      markActive(mid, false)
    }
  }

  const runAll = async () => {
    if (running || !fixRows.length) return
    abort.current = false
    setRunning(true)
    setMessage(null)
    setMessageTone('info')
    const queue = [...fixRows].sort(
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
    // Binance Fix already patched rows locally; full list rescore is the slow part.
    if (kind === 'trades') {
      void refresh()
    }
  }

  const fixTitle =
    kind === 'trades'
      ? 'Backfill trades.parquet locally from Polymarket Data API (no VPS)'
      : kind === 'binance-price'
        ? 'Fill binance_price_orderbook.parquet from Binance klines (local, no VPS)'
        : 'Fill binance_trades.parquet from Binance aggTrades (local, no VPS)'

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
              disabled={loading || fixRows.length === 0}
              onClick={() => void runAll()}
              title={fixTitle}
            >
              Fix
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

      <div className="pmq-tags" role="group" aria-label={`${title} grade filter`}>
        {TAPE_GRADE_FILTERS.map((tag) => {
          const n = tag === 'all' ? stats.total : counts[tag] || 0
          const active = gradeFilter === tag
          return (
            <button
              key={tag}
              type="button"
              className={`pmq-tag${active ? ' active' : ''}${tag !== 'all' ? ` grade-${tag}` : ''}`}
              disabled={loading || running}
              onClick={() => setGradeFilter(tag)}
              title={
                tag === 'all'
                  ? `Show all ${stats.total} markets`
                  : tag === 'unchecked'
                    ? `Not Fix-stamped in meta yet (${n})`
                    : `Show ${tag} only (${n})`
              }
            >
              {tag}
              <span className="pmq-tag-n">{n}</span>
            </button>
          )
        })}
      </div>

      <div
        className="pmq-meter"
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={coveragePct}
        aria-label={`${title} healthy coverage`}
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
        ) : (
          <>
            <span className="pmq-dot" aria-hidden>
              ·
            </span>
            <span className="pmq-binance-counts" title={`Grade counts for ${fileHint}`}>
              {(['great', 'good', 'ok', 'low', 'bad'] as const)
                .filter((k) => (counts[k] || 0) > 0)
                .map((k) => `${k[0]}${counts[k]}`)
                .join(' ')}
            </span>
          </>
        )}
      </div>

      {message ? <p className={`pmq-msg ${messageTone}`}>{message}</p> : null}

      <div
        className={`pmq-list${!loading && !visible.length ? ' empty' : ''}`}
        aria-label={`${title} markets`}
      >
        {loading && !rows.length ? (
          <div className="pmq-empty">Scoring {fileHint}…</div>
        ) : !visible.length ? (
          <div className="pmq-empty ok">
            {stats.total > 0
              ? gradeFilter === 'unchecked'
                ? 'No unchecked data'
                : gradeFilter === 'all'
                  ? 'No markets'
                  : `No ${gradeFilter}`
              : 'No markets'}
          </div>
        ) : (
          groups.map(([date, list]) => (
            <div key={date} className="pmq-group">
              <div className="pmq-group-head">
                {date}
                <span>{list.length}</span>
              </div>
              {list.map((m) => {
                const grade = (issueGrade(m) || 'unchecked').toLowerCase()
                const active = activeIds.has(m.market_id)
                const checked = isTapeChecked(kind, m)
                const detail = [
                  kind === 'binance-price' && m.has_price === false ? 'no price' : null,
                  kind === 'binance-trades' && m.has_trades === false ? 'no trades' : null,
                  kind === 'trades' && m.has_trades === false ? 'no trades' : null,
                  checked ? 'checked in meta' : null,
                  kind === 'binance-price'
                    ? `gap ${formatQuietMs(m.max_gap_ms)}`
                    : `quiet ${formatQuietMs(m.max_trade_quiet_ms)}`,
                ]
                  .filter(Boolean)
                  .join(' · ')
                return (
                  <div
                    key={m.market_id}
                    ref={active ? activeRowRef : undefined}
                    className={`pmq-row${active ? ' active' : ''}${checked ? ' checked' : ''}`}
                  >
                    <div className="pmq-row-main" title={detail || m.market_id}>
                      <span className="pmq-time">
                        {formatSlotLabel(m.time_et || '', m.start_time, m.end_time)}
                      </span>
                      <span className="pmq-id">{m.market_id}</span>
                      <span className={`pmq-grade health-${grade}`}>{grade}</span>
                      {checked ? (
                        <span className="pmq-checked" title="Fix-stamped in meta.json" aria-label="Checked">
                          <CheckIcon />
                        </span>
                      ) : null}
                    </div>
                    <button
                      type="button"
                      className="pmq-run-one"
                      disabled={running}
                      onClick={() => void runOne(m.market_id).catch(() => undefined)}
                      title={`Fix ${fileHint} for ${m.market_id}`}
                    >
                      {active ? <span className="pmq-spinner" aria-label="Fixing" /> : 'Fix'}
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
  const [rescoring, setRescoring] = useState(false)
  const [rescoreMsg, setRescoreMsg] = useState<string | null>(null)
  const [rescoreTone, setRescoreTone] = useState<'info' | 'ok' | 'err'>('info')

  const runRescoreAll = async () => {
    if (rescoring) return
    setRescoring(true)
    setRescoreTone('info')
    setRescoreMsg('Rescoring all local markets…')
    try {
      const res = await api.rescoreAllHealth()
      const parts = (['great', 'good', 'ok', 'low', 'bad', 'unchecked'] as const)
        .map((k) => {
          const n = res.counts?.[k] || 0
          return n > 0 ? `${k} ${n}` : null
        })
        .filter(Boolean)
      const errN = res.n_errors || 0
      setRescoreTone(errN ? 'err' : 'ok')
      setRescoreMsg(
        `Rescored ${res.updated.toLocaleString()}/${res.targets.toLocaleString()}` +
          (parts.length ? ` · ${parts.join(' · ')}` : '') +
          (errN ? ` · ${errN} errors` : ''),
      )
    } catch (err) {
      setRescoreTone('err')
      setRescoreMsg(err instanceof Error ? err.message : 'Rescore failed')
    } finally {
      setRescoring(false)
    }
  }

  return (
    <div className="pmdata-page">
      <header className="pmdata-bar">
        <div className="pmdata-bar-text">
          <h1>Data Health</h1>
          <p>
            Fill <code>pm_orderbooks</code> / <code>pm_chainlink_price</code>, then restamp health.
            Trades / Binance Price / Binance Trades fix locally (no VPS). Oldest first · live excluded.
          </p>
          {rescoreMsg ? <p className={`pmdata-bar-msg ${rescoreTone}`}>{rescoreMsg}</p> : null}
        </div>
        <div className="pmdata-bar-actions">
          <button
            type="button"
            className="pmq-btn primary"
            disabled={rescoring}
            onClick={() => void runRescoreAll()}
            title="Restamp meta.data_health for all local history markets (no VPS)"
          >
            {rescoring ? 'Rescoring…' : 'Rescore'}
          </button>
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
        <TapeHealthPanel kind="trades" />
        <TapeHealthPanel kind="binance-price" />
        <TapeHealthPanel kind="binance-trades" />
      </div>
    </div>
  )
}
