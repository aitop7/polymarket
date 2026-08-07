import type { MarketSummary } from '../api'

type Props = {
  mode: 'monitor' | 'paper'
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
}

export default function ControlSidebar(props: Props) {
  const {
    mode,
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
  } = props

  return (
    <aside className="control-sidebar control-sidebar-left">
      <div className="sidebar-section">
        <div className="sidebar-heading">Data</div>
        <label className="sidebar-label">Dataset</label>
        <select value={split} onChange={(e) => onSplit(e.target.value)} disabled={indexing}>
          <option value="validation">Validation</option>
          <option value="test">Test</option>
          <option value="train">Train</option>
        </select>

        <label className="sidebar-label">Date (ET)</label>
        <input
          type="date"
          value={selectedDate}
          min={dateMin || undefined}
          max={dateMax || undefined}
          disabled={!dateMin}
          onChange={(e) => onDate(e.target.value)}
        />

        <label className="sidebar-label">Time window (ET)</label>
        <select
          value={selectedTime}
          disabled={!markets.length || indexing}
          onChange={(e) => onTime(e.target.value)}
        >
          {markets.length === 0 && (
            <option value="">{indexing ? 'Loading slots…' : 'No markets this day'}</option>
          )}
          {markets.map((m) => (
            <option key={m.market_id} value={m.time_et || ''}>
              {formatSlotLabel(m.time_et || '', m.start_time, m.end_time)}
            </option>
          ))}
        </select>

        {indexing ? (
          <div className="sidebar-badge">Indexing calendar…</div>
        ) : (
          dateMin &&
          dateMax && (
            <div className="sidebar-meta">
              {dateMin} → {dateMax}
            </div>
          )
        )}
      </div>

      <div className="sidebar-section sidebar-section-last">
        <div className="sidebar-heading">Playback</div>
        <label className="sidebar-label">Speed</label>
        <select value={speed} onChange={(e) => onSpeed(Number(e.target.value))}>
          <option value={1}>1x · Normal</option>
          <option value={5}>5x</option>
          <option value={10}>10x</option>
          <option value={30}>30x</option>
          <option value={60}>60x</option>
          <option value={120}>120x</option>
        </select>

        {mode === 'paper' && (
          <>
            <label className="sidebar-label">Strategy</label>
            <select value={strategy} onChange={(e) => onStrategy(e.target.value)}>
              <option value="none">Manual only</option>
              <option value="lgbm_edge">LightGBM edge</option>
              <option value="edge_threshold">Edge threshold</option>
            </select>
          </>
        )}

        <div className="sidebar-btn-row">
          {!playing ? (
            <button
              type="button"
              className="sidebar-btn primary"
              onClick={onPlay}
              disabled={!marketId || indexing}
            >
              Play
            </button>
          ) : paused ? (
            <button type="button" className="sidebar-btn primary" onClick={onResume}>
              Resume
            </button>
          ) : (
            <button type="button" className="sidebar-btn" onClick={onPause}>
              Pause
            </button>
          )}
          <button
            type="button"
            className="sidebar-btn danger"
            onClick={onStop}
            disabled={!playing && !paused}
          >
            Stop
          </button>
        </div>

        <div className="sidebar-btn-row">
          <button type="button" className="sidebar-btn" onClick={onPrev} disabled={!hasPrev || indexing}>
            ← Prev
          </button>
          <button type="button" className="sidebar-btn" onClick={onNext} disabled={!hasNext || indexing}>
            Next →
          </button>
        </div>
      </div>
    </aside>
  )
}
