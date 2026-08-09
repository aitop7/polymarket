import { useEffect, useMemo, useRef, useState } from 'react'
import type { MarketSummary } from '../api'

function outcomeTone(m: MarketSummary): 'up' | 'down' | 'pending' {
  if (m.winner === 1) return 'up'
  if (m.winner === 0) return 'down'
  if (m.closed === false) return 'pending'
  return 'pending'
}

function outcomeLabel(tone: 'up' | 'down' | 'pending'): string {
  if (tone === 'up') return 'Up'
  if (tone === 'down') return 'Down'
  return '—'
}

function formatClock(ms: number): string {
  const sec = Math.max(0, Math.floor(ms / 1000))
  const m = Math.floor(sec / 60)
  const s = sec % 60
  return `${m}:${String(s).padStart(2, '0')}`
}

function IconPrev() {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden>
      <path fill="currentColor" d="M6 6h2v12H6V6zm3.5 6 8.5 6V6l-8.5 6z" />
    </svg>
  )
}

function IconNext() {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden>
      <path fill="currentColor" d="M16 6h2v12h-2V6zM6 18l8.5-6L6 6v12z" />
    </svg>
  )
}

function IconPlay() {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden>
      <path fill="currentColor" d="M8 5v14l11-7L8 5z" />
    </svg>
  )
}

function IconPause() {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden>
      <path fill="currentColor" d="M6 5h4v14H6V5zm8 0h4v14h-4V5z" />
    </svg>
  )
}

function IconStop() {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden>
      <path fill="currentColor" d="M6 6h12v12H6V6z" />
    </svg>
  )
}

type Props = {
  mode: 'monitor' | 'paper'
  liveActive: boolean
  onToggleLive: () => void
  liveLabel?: string
  liveInterval: number
  onLiveInterval: (s: number) => void
  collection: 'before_twap' | 'twap'
  onCollection: (c: 'before_twap' | 'twap') => void
  split: string
  onSplit: (s: string) => void
  indexing: boolean
  dateMin: string
  dateMax: string
  selectedDate: string
  onDate: (d: string) => void
  selectedTime: string
  markets: MarketSummary[]
  onTime: (t: string) => void
  formatSlotLabel: (timeEt: string, startMs?: number, endMs?: number) => string
  speed: number
  onSpeed: (n: number) => void
  playing: boolean
  paused: boolean
  onPlay: () => void
  onPause: () => void
  onResume: () => void
  onStop: () => void
  marketId: string
  hasPrev: boolean
  hasNext: boolean
  onPrev: () => void
  onNext: () => void
  strategy: string
  onStrategy: (s: string) => void
  /** Market window for timeline scrubbing */
  marketStartMs?: number | null
  marketEndMs?: number | null
  /** Current playhead (ms); null = idle / full market */
  playheadMs?: number | null
  onSeek?: (timestampMs: number) => void
}

export default function ControlSidebar(props: Props) {
  const {
    mode,
    liveActive,
    onToggleLive,
    liveLabel,
    liveInterval,
    onLiveInterval,
    collection,
    onCollection,
    split,
    onSplit,
    indexing,
    dateMin,
    dateMax,
    selectedDate,
    onDate,
    selectedTime,
    markets,
    onTime,
    formatSlotLabel,
    speed,
    onSpeed,
    playing,
    paused,
    onPlay,
    onPause,
    onResume,
    onStop,
    marketId,
    hasPrev,
    hasNext,
    onPrev,
    onNext,
    strategy,
    onStrategy,
    marketStartMs,
    marketEndMs,
    playheadMs,
    onSeek,
  } = props

  const histDisabled = liveActive || indexing
  const beforeTwap = collection === 'before_twap'
  const start = marketStartMs != null && Number.isFinite(marketStartMs) ? marketStartMs : null
  const end = marketEndMs != null && Number.isFinite(marketEndMs) ? marketEndMs : null
  const span = start != null && end != null && end > start ? end - start : 0
  const activeReplay = playing || paused

  const [dragTs, setDragTs] = useState<number | null>(null)
  const dragging = useRef(false)

  const displayTs = dragTs ?? playheadMs ?? start
  const progress =
    span > 0 && displayTs != null && start != null
      ? Math.min(1, Math.max(0, (displayTs - start) / span))
      : 0

  useEffect(() => {
    if (!dragging.current) setDragTs(null)
  }, [playheadMs])

  const elapsedLabel = useMemo(() => {
    if (start == null || displayTs == null) return '0:00'
    return formatClock(displayTs - start)
  }, [start, displayTs])

  const durationLabel = useMemo(() => {
    if (span <= 0) return '5:00'
    return formatClock(span)
  }, [span])

  const seekDisabled = histDisabled || !marketId || span <= 0 || !onSeek

  const onSliderInput = (value: number) => {
    if (start == null || span <= 0) return
    const ts = Math.round(start + (value / 1000) * span)
    setDragTs(ts)
  }

  const commitSeek = (value: number) => {
    if (start == null || span <= 0 || !onSeek) return
    const ts = Math.round(start + (value / 1000) * span)
    dragging.current = false
    setDragTs(null)
    onSeek(ts)
  }

  return (
    <div className={`control-sidebar control-sidebar-embedded${liveActive ? ' live-mode' : ''}`}>
      <div className={`sidebar-section mode-section${liveActive ? ' sidebar-section-last' : ''}`}>
        <div className="sidebar-heading mode-heading">
          <span>Mode</span>
          <span className={`mode-current-pill${liveActive ? ' live' : ''}`}>
            {liveActive ? (
              <>
                <i /> Live
              </>
            ) : (
              'History'
            )}
          </span>
        </div>

        {liveActive ? (
          <button type="button" className="mode-nav-btn history" onClick={onToggleLive}>
            <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden>
              <path
                fill="currentColor"
                d="M13 3a9 9 0 1 0 8.95 10h-2.02A7 7 0 1 1 13 5V3zm1 0v8.6l5.4 3.2-.9 1.5L12 12V3h2z"
              />
            </svg>
            <span className="mode-nav-copy">
              <strong>History</strong>
              <small>Browse past markets</small>
            </span>
            <span className="mode-nav-chevron" aria-hidden>
              →
            </span>
          </button>
        ) : (
          <button type="button" className="mode-nav-btn live" onClick={onToggleLive}>
            <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden>
              <path
                fill="currentColor"
                d="M12 5a7 7 0 0 1 7 7c0 3.3-2.3 6.1-5.4 6.8L12 22l-1.6-3.2A7 7 0 0 1 5 12a7 7 0 0 1 7-7zm0 2a5 5 0 1 0 .01 10.01A5 5 0 0 0 12 7zm0 2.5a2.5 2.5 0 1 1 0 5 2.5 2.5 0 0 1 0-5z"
              />
            </svg>
            <span className="mode-nav-copy">
              <strong>Live market</strong>
              <small>Watch current window</small>
            </span>
            <span className="mode-nav-chevron" aria-hidden>
              →
            </span>
          </button>
        )}

        {liveActive && (
          <>
            <div className="mode-field-row">
              <span className="mode-field-label">Fetch</span>
              <select
                className="mode-field-control"
                value={liveInterval}
                onChange={(e) => onLiveInterval(Number(e.target.value))}
                aria-label="Fetch interval"
              >
                <option value={0.1}>0.1s</option>
                <option value={0.2}>0.2s</option>
                <option value={0.5}>0.5s</option>
                <option value={1}>1s</option>
                <option value={1.5}>1.5s</option>
                <option value={2}>2s</option>
              </select>
            </div>
            {liveLabel && (
              <div className="mode-live-meta" title={liveLabel}>
                {liveLabel}
              </div>
            )}
          </>
        )}
      </div>

      {!liveActive && (
      <>
      <div className="sidebar-section data-section">
        <div className="sidebar-heading data-heading">
          <span>Data</span>
          {indexing ? (
            <span className="data-status indexing">Indexing…</span>
          ) : (
            dateMin &&
            dateMax &&
            !liveActive && <span className="data-status">{markets.length} slots</span>
          )}
        </div>

        <div className="data-segment" role="group" aria-label="Collection">
          <button
            type="button"
            className={`data-segment-btn${collection === 'twap' ? ' active' : ''}`}
            disabled={histDisabled}
            onClick={() => onCollection('twap')}
            aria-pressed={collection === 'twap'}
          >
            TWAP
          </button>
          <button
            type="button"
            className={`data-segment-btn${collection === 'before_twap' ? ' active' : ''}`}
            disabled={histDisabled}
            onClick={() => onCollection('before_twap')}
            aria-pressed={collection === 'before_twap'}
          >
            Before
          </button>
        </div>

        {beforeTwap ? (
          <div className="data-field-row">
            <span className="data-field-label">Split</span>
            <select
              className="data-field-control"
              value={split}
              onChange={(e) => onSplit(e.target.value)}
              disabled={histDisabled}
              aria-label="Dataset split"
            >
              <option value="validation">Validation</option>
              <option value="test">Test</option>
              <option value="train">Train</option>
            </select>
          </div>
        ) : (
          <div className="data-path" title="E:\DataSets\poly\live">
            <svg viewBox="0 0 24 24" width="12" height="12" aria-hidden>
              <path
                fill="currentColor"
                d="M10 4H4a2 2 0 0 0-2 2v12c0 1.1.9 2 2 2h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-8l-2-2z"
              />
            </svg>
            <span>E:\DataSets\poly\live</span>
          </div>
        )}

        <div className="data-field-row">
          <span className="data-field-label">Date</span>
          <input
            className="data-field-control"
            type="date"
            value={selectedDate}
            min={dateMin || undefined}
            max={dateMax || undefined}
            disabled={!dateMin || liveActive}
            onChange={(e) => onDate(e.target.value)}
            aria-label="Date ET"
          />
        </div>

        <div className="data-windows-head">
          <span>Window (ET)</span>
          {dateMin && dateMax && !liveActive && (
            <span className="data-windows-range">
              {dateMin.slice(5)} → {dateMax.slice(5)}
            </span>
          )}
        </div>
        <div
          className={`time-window-list${histDisabled || !markets.length ? ' disabled' : ''}`}
          role="listbox"
          aria-label="Time window"
          aria-disabled={histDisabled || !markets.length}
        >
          {markets.length === 0 ? (
            <div className="time-window-empty">
              {indexing ? 'Loading slots…' : 'No markets this day'}
            </div>
          ) : (
            markets.map((m) => {
              const tone = outcomeTone(m)
              const active = (m.time_et || '') === selectedTime
              return (
                <button
                  key={m.market_id}
                  type="button"
                  role="option"
                  aria-selected={active}
                  className={`time-window-item${active ? ' active' : ''}`}
                  disabled={histDisabled}
                  onClick={() => onTime(m.time_et || '')}
                >
                  <span className="time-window-label">
                    {formatSlotLabel(m.time_et || '', m.start_time, m.end_time)}
                  </span>
                  <span className={`time-window-badge ${tone}`} title={`Outcome: ${outcomeLabel(tone)}`}>
                    {tone === 'up' ? '▲ Up' : tone === 'down' ? '▼ Down' : '—'}
                  </span>
                </button>
              )
            })
          )}
        </div>
      </div>

      <div className="sidebar-section sidebar-section-last playback-section">
        <div className="sidebar-heading playback-heading">
          <span>Playback</span>
          {activeReplay && (
            <span className={`playback-status${paused ? ' paused' : ''}`}>
              {paused ? 'Paused' : 'Playing'}
            </span>
          )}
        </div>

        <div className="playback-transport">
          <button
            type="button"
            className="playback-icon-btn"
            onClick={onPrev}
            disabled={!hasPrev || histDisabled}
            title="Previous market"
            aria-label="Previous market"
          >
            <IconPrev />
          </button>

          {!playing ? (
            <button
              type="button"
              className="playback-icon-btn primary"
              onClick={onPlay}
              disabled={!marketId || histDisabled}
              title="Play"
              aria-label="Play"
            >
              <IconPlay />
            </button>
          ) : paused ? (
            <button
              type="button"
              className="playback-icon-btn primary"
              onClick={onResume}
              disabled={liveActive}
              title="Resume"
              aria-label="Resume"
            >
              <IconPlay />
            </button>
          ) : (
            <button
              type="button"
              className="playback-icon-btn primary"
              onClick={onPause}
              disabled={liveActive}
              title="Pause"
              aria-label="Pause"
            >
              <IconPause />
            </button>
          )}

          <button
            type="button"
            className="playback-icon-btn danger"
            onClick={onStop}
            disabled={liveActive || (!playing && !paused && playheadMs == null)}
            title="Stop"
            aria-label="Stop"
          >
            <IconStop />
          </button>

          <button
            type="button"
            className="playback-icon-btn"
            onClick={onNext}
            disabled={!hasNext || histDisabled}
            title="Next market"
            aria-label="Next market"
          >
            <IconNext />
          </button>

          <select
            className="playback-speed"
            value={speed}
            onChange={(e) => onSpeed(Number(e.target.value))}
            disabled={liveActive}
            title="Playback speed"
            aria-label="Playback speed"
          >
            <option value={1}>1×</option>
            <option value={5}>5×</option>
            <option value={10}>10×</option>
            <option value={30}>30×</option>
            <option value={60}>60×</option>
            <option value={120}>120×</option>
          </select>
        </div>

        <div className="playback-timeline">
          <span className="playback-time">{elapsedLabel}</span>
          <input
            type="range"
            className="playback-scrubber"
            min={0}
            max={1000}
            step={1}
            value={Math.round(progress * 1000)}
            disabled={seekDisabled}
            aria-label="Seek in market"
            style={{
              background: `linear-gradient(to right, var(--accent) ${progress * 100}%, #e5e7eb ${progress * 100}%)`,
            }}
            onPointerDown={() => {
              dragging.current = true
            }}
            onChange={(e) => onSliderInput(Number(e.target.value))}
            onPointerUp={(e) => commitSeek(Number((e.target as HTMLInputElement).value))}
            onKeyUp={(e) => commitSeek(Number((e.target as HTMLInputElement).value))}
          />
          <span className="playback-time">{durationLabel}</span>
        </div>

        {mode === 'paper' && (
          <>
            <label className="sidebar-label">Strategy</label>
            <select
              value={strategy}
              onChange={(e) => onStrategy(e.target.value)}
              disabled={liveActive}
            >
              <option value="none">Manual only</option>
              <option value="lgbm_edge">LightGBM edge</option>
              <option value="edge_threshold">Edge threshold</option>
            </select>
          </>
        )}
      </div>
      </>
      )}
    </div>
  )
}
