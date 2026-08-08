type Props = {
  /** Up/Down from meta.winner; not_closed when meta.closed is false / unresolved */
  outcome: 'Up' | 'Down' | 'not_closed' | null
  subtitle: string
}

export default function OutcomeCard({ outcome, subtitle }: Props) {
  const resolved = outcome === 'Up' || outcome === 'Down'
  const up = outcome === 'Up'
  const notClosed = outcome === 'not_closed' || outcome == null

  return (
    <div
      className={`outcome-card ${resolved ? (up ? 'up' : 'down') : 'pending'}${
        notClosed ? ' not-closed' : ''
      }`}
    >
      <div className="outcome-card-icon" aria-hidden>
        {resolved ? (
          <svg viewBox="0 0 24 24" width="28" height="28" fill="none">
            <circle cx="12" cy="12" r="12" className="outcome-card-icon-bg" />
            <path
              d="M7 12.5l3.2 3.2L17 8.8"
              stroke="#fff"
              strokeWidth="2.4"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        ) : (
          <svg viewBox="0 0 24 24" width="28" height="28" fill="none">
            <circle cx="12" cy="12" r="12" className="outcome-card-icon-bg" />
            <path
              d="M8 12h8"
              stroke="#fff"
              strokeWidth="2.4"
              strokeLinecap="round"
            />
          </svg>
        )}
      </div>
      <div className="outcome-card-title">
        {resolved ? `Outcome: ${outcome}` : 'Not closed'}
      </div>
      <div className="outcome-card-sub">{subtitle}</div>
    </div>
  )
}
