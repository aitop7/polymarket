type Props = {
  collapsed: boolean
  onToggle: () => void
  label?: string
}

/** Chevron: down when expanded (click to collapse), right when collapsed (click to expand). */
function ChevronIcon({ collapsed }: { collapsed: boolean }) {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d={collapsed ? 'M9 6l6 6-6 6' : 'M6 9l6 6 6-6'}
        stroke="currentColor"
        strokeWidth="2.2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}

export default function ChartCollapseButton({ collapsed, onToggle, label = 'chart' }: Props) {
  return (
    <button
      type="button"
      className="chart-collapse"
      aria-label={collapsed ? `Expand ${label}` : `Collapse ${label}`}
      aria-expanded={!collapsed}
      title={collapsed ? 'Expand' : 'Collapse'}
      onClick={onToggle}
    >
      <ChevronIcon collapsed={collapsed} />
    </button>
  )
}
