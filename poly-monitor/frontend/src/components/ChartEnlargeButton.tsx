type Props = {
  onClick: () => void
  label?: string
  /** Close (X) instead of zoom-in when already in the large view. */
  mode?: 'enlarge' | 'close'
}

function ZoomInIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <circle cx="10.5" cy="10.5" r="6.5" stroke="currentColor" strokeWidth="2" />
      <path
        d="M16 16l5 5M10.5 7.5v6M7.5 10.5h6"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
      />
    </svg>
  )
}

function CloseIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M6 6l12 12M18 6L6 18"
        stroke="currentColor"
        strokeWidth="2.2"
        strokeLinecap="round"
      />
    </svg>
  )
}

/** Zoom-in opens a large detail chart; close dismisses the lightbox. */
export default function ChartEnlargeButton({
  onClick,
  label = 'chart',
  mode = 'enlarge',
}: Props) {
  const closing = mode === 'close'
  return (
    <button
      type="button"
      className={`chart-enlarge${closing ? ' chart-enlarge-close' : ''}`}
      aria-label={closing ? `Close large ${label}` : `Enlarge ${label}`}
      title={closing ? 'Close' : 'Large chart'}
      onClick={onClick}
    >
      {closing ? <CloseIcon /> : <ZoomInIcon />}
    </button>
  )
}
