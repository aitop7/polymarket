import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
  type RefObject,
} from 'react'

export type TimeDomain = [number, number]
export type YDomain = [number, number]
export type ChartZone = 'price' | 'time' | 'plot'

export type ChartMargin = {
  top: number
  right: number
  left: number
  bottom: number
}

export function clampDomain(domain: TimeDomain, full: TimeDomain): TimeDomain {
  const [f0, f1] = full
  const span = Math.max(1, f1 - f0)
  let [a, b] = domain
  if (b < a) [a, b] = [b, a]
  const width = Math.max(span * 0.02, b - a)
  let mid = (a + b) / 2
  if (mid - width / 2 < f0) mid = f0 + width / 2
  if (mid + width / 2 > f1) mid = f1 - width / 2
  return [mid - width / 2, mid + width / 2]
}

export function domainEqual(a: TimeDomain, b: TimeDomain, eps = 1): boolean {
  return Math.abs(a[0] - b[0]) < eps && Math.abs(a[1] - b[1]) < eps
}

type DragState = {
  pointerId: number
  x: number
  y: number
  xDomain: TimeDomain
  yDomain: YDomain
  zone: ChartZone
  /** Fraction of plot width at pointer (0–1) when drag began — zoom anchor. */
  xFrac: number
  /** Fraction of plot height at pointer (0–1) when drag began — zoom anchor. */
  yFrac: number
}

type PlotGeom = {
  plotLeft: number
  plotTop: number
  plotW: number
  plotH: number
  plotRight: number
  plotBottom: number
}

function plotGeom(
  el: HTMLElement,
  margin: ChartMargin,
  yAxisWidth: number,
): PlotGeom {
  const rect = el.getBoundingClientRect()
  const plotLeft = rect.left + margin.left
  const plotTop = rect.top + margin.top
  const plotRight = rect.right - margin.right - yAxisWidth
  const plotBottom = rect.bottom - margin.bottom
  return {
    plotLeft,
    plotTop,
    plotRight,
    plotBottom,
    plotW: Math.max(1, plotRight - plotLeft),
    plotH: Math.max(1, plotBottom - plotTop),
  }
}

function clientToXFrac(clientX: number, g: PlotGeom): number {
  return Math.min(1, Math.max(0, (clientX - g.plotLeft) / g.plotW))
}

function clientToYFrac(clientY: number, g: PlotGeom): number {
  return Math.min(1, Math.max(0, (clientY - g.plotTop) / g.plotH))
}

/** Zoom X around an anchor fraction within the current domain. */
export function zoomXAround(
  domain: TimeDomain,
  full: TimeDomain,
  factor: number,
  frac: number,
): TimeDomain {
  const [x0, x1] = domain
  const span = Math.max(1, x1 - x0)
  const fullSpan = Math.max(1, full[1] - full[0])
  const minSpan = fullSpan * 0.02
  const nextSpan = Math.min(fullSpan, Math.max(minSpan, span * factor))
  const f = Math.min(1, Math.max(0, frac))
  const anchor = x0 + f * span
  return clampDomain([anchor - f * nextSpan, anchor - f * nextSpan + nextSpan], full)
}

/** Zoom Y around an anchor fraction (0 = top of plot, 1 = bottom). */
export function zoomYAround(
  domain: YDomain,
  factor: number,
  frac: number,
  minSpan: number,
  clampY?: (d: YDomain) => YDomain,
): YDomain {
  const [y0, y1] = domain
  const span = Math.max(minSpan, y1 - y0)
  const nextSpan = Math.max(minSpan, span * factor)
  const f = Math.min(1, Math.max(0, frac))
  // Screen y grows downward; domain high is at top (frac 0).
  const anchor = y1 - f * span
  const next1 = anchor + f * nextSpan
  const next0 = next1 - nextSpan
  const raw: YDomain = [next0, next1]
  return clampY ? clampY(raw) : raw
}

export type UseChartViewportArgs = {
  xDomain: TimeDomain
  xFullDomain: TimeDomain
  xDefaultDomain: TimeDomain
  onXDomainChange: (next: TimeDomain) => void
  onXDomainReset?: () => void
  /** Current Y domain (auto or manual). */
  yDomain: YDomain
  /** When set, plot/axis Y gestures are enabled. Pass null to clear manual Y zoom. */
  setYDomain?: (next: YDomain | null) => void
  clampY?: (d: YDomain) => YDomain
  minYSpan?: number
  margin: ChartMargin
  yAxisWidth: number
  /** Enable vertical pan/zoom (price charts). Default true when setYDomain provided. */
  enableY?: boolean
  /** True when the caller has a manual Y zoom (not auto-scale). */
  yManual?: boolean
  /** When false, gestures are no-ops (still safe to call the hook). */
  enabled?: boolean
}

export type ChartViewportBind = {
  ref: RefObject<HTMLDivElement | null>
  tabIndex: number
  onPointerDown: (ev: ReactPointerEvent<HTMLDivElement>) => void
  onPointerMove: (ev: ReactPointerEvent<HTMLDivElement>) => void
  onPointerUp: (ev: ReactPointerEvent<HTMLDivElement>) => void
  onPointerCancel: (ev: ReactPointerEvent<HTMLDivElement>) => void
  onPointerLeave: () => void
  onDoubleClick: () => void
  title: string
}

export function useChartViewport(args: UseChartViewportArgs) {
  const {
    xDomain,
    xFullDomain,
    xDefaultDomain,
    onXDomainChange,
    onXDomainReset,
    yDomain,
    setYDomain,
    clampY,
    minYSpan = 1,
    margin,
    yAxisWidth,
    enableY = Boolean(setYDomain),
    yManual = false,
    enabled = true,
  } = args

  const wrapRef = useRef<HTMLDivElement | null>(null)
  const dragRef = useRef<DragState | null>(null)
  const hoveredRef = useRef(false)
  const enabledRef = useRef(enabled)
  const [hoverZone, setHoverZone] = useState<ChartZone>('plot')
  const [dragging, setDragging] = useState(false)

  // Keep latest domains in refs for wheel handler (avoids stale closures).
  const xDomainRef = useRef(xDomain)
  const yDomainRef = useRef(yDomain)
  const xFullRef = useRef(xFullDomain)
  const onXChangeRef = useRef(onXDomainChange)
  const setYRef = useRef(setYDomain)
  const clampYRef = useRef(clampY)
  const enableYRef = useRef(enableY)
  const minYSpanRef = useRef(minYSpan)
  const marginRef = useRef(margin)
  const yAxisWidthRef = useRef(yAxisWidth)

  xDomainRef.current = xDomain
  yDomainRef.current = yDomain
  xFullRef.current = xFullDomain
  onXChangeRef.current = onXDomainChange
  setYRef.current = setYDomain
  clampYRef.current = clampY
  enableYRef.current = enableY
  minYSpanRef.current = minYSpan
  marginRef.current = margin
  yAxisWidthRef.current = yAxisWidth
  enabledRef.current = enabled

  const hitZone = useCallback((clientX: number, clientY: number): ChartZone => {
    const el = wrapRef.current
    if (!el) return 'plot'
    const g = plotGeom(el, marginRef.current, yAxisWidthRef.current)
    // Bottom-right corner is the reset control.
    if (clientX >= g.plotRight && clientY >= g.plotBottom) return 'plot'
    if (enableYRef.current && clientX >= g.plotRight) return 'price'
    if (clientY >= g.plotBottom) return 'time'
    return 'plot'
  }, [])

  const resetZoom = useCallback(() => {
    if (!enabled) return
    if (onXDomainReset) onXDomainReset()
    else onXDomainChange(xDefaultDomain)
    setYDomain?.(null)
  }, [enabled, onXDomainReset, onXDomainChange, xDefaultDomain, setYDomain])

  const onPointerDown = (ev: ReactPointerEvent<HTMLDivElement>) => {
    if (!enabled) return
    if (ev.button !== 0) return
    ev.preventDefault()
    const el = wrapRef.current
    if (!el) return
    const zone = hitZone(ev.clientX, ev.clientY)
    setHoverZone(zone)
    const g = plotGeom(el, margin, yAxisWidth)
    ;(ev.currentTarget as HTMLElement).setPointerCapture(ev.pointerId)
    dragRef.current = {
      pointerId: ev.pointerId,
      x: ev.clientX,
      y: ev.clientY,
      xDomain: [...xDomain] as TimeDomain,
      yDomain: [...yDomain] as YDomain,
      zone,
      xFrac: clientToXFrac(ev.clientX, g),
      yFrac: clientToYFrac(ev.clientY, g),
    }
    setDragging(true)
  }

  const onPointerMove = (ev: ReactPointerEvent<HTMLDivElement>) => {
    if (!enabled) return
    const zone = hitZone(ev.clientX, ev.clientY)
    if (!dragRef.current) {
      setHoverZone((z) => (z === zone ? z : zone))
    }
    const drag = dragRef.current
    const el = wrapRef.current
    if (!drag || !el) return
    const g = plotGeom(el, margin, yAxisWidth)
    const dx = ev.clientX - drag.x
    const dy = ev.clientY - drag.y
    const fullXSpan = Math.max(1, xFullDomain[1] - xFullDomain[0])
    const minXSpan = fullXSpan * 0.02

    if (drag.zone === 'time') {
      const [x0, x1] = drag.xDomain
      const xSpan = Math.max(1, x1 - x0)
      const factor = Math.exp((-dx / g.plotW) * 2.2)
      const nextSpan = Math.min(fullXSpan, Math.max(minXSpan, xSpan * factor))
      const f = drag.xFrac
      const anchor = x0 + f * xSpan
      onXDomainChange(
        clampDomain([anchor - f * nextSpan, anchor - f * nextSpan + nextSpan], xFullDomain),
      )
      return
    }

    if (drag.zone === 'price' && enableY && setYDomain) {
      // Drag up → zoom in (TradingView price-scale feel), anchored at drag start.
      const factor = Math.exp((dy / g.plotH) * 2.2)
      setYDomain(zoomYAround(drag.yDomain, factor, drag.yFrac, minYSpan, clampY))
      return
    }

    if (drag.zone === 'plot') {
      const [x0, x1] = drag.xDomain
      const xSpan = x1 - x0
      const xShift = (-dx / g.plotW) * xSpan
      onXDomainChange(clampDomain([x0 + xShift, x1 + xShift], xFullDomain))

      if (enableY && setYDomain) {
        const [y0, y1] = drag.yDomain
        const ySpan = y1 - y0
        const yShift = (dy / g.plotH) * ySpan
        const raw: YDomain = [y0 + yShift, y1 + yShift]
        setYDomain(clampY ? clampY(raw) : raw)
      }
    }
  }

  const onPointerUp = (ev: ReactPointerEvent<HTMLDivElement>) => {
    if (dragRef.current && dragRef.current.pointerId !== ev.pointerId) return
    dragRef.current = null
    setDragging(false)
    try {
      ;(ev.currentTarget as HTMLElement).releasePointerCapture(ev.pointerId)
    } catch {
      /* ignore */
    }
  }

  const onPointerLeave = () => {
    hoveredRef.current = false
    if (!dragRef.current) setHoverZone('plot')
  }

  // Non-passive wheel: zoom X around cursor; Shift+wheel zooms Y when enabled.
  useEffect(() => {
    const el = wrapRef.current
    if (!el) return

    const onWheel = (ev: WheelEvent) => {
      if (!enabledRef.current) return
      if (!hoveredRef.current && !el.contains(ev.target as Node)) return
      // Only intercept when pointer is over this chart.
      if (!el.contains(ev.target as Node) && !hoveredRef.current) return

      const g = plotGeom(el, marginRef.current, yAxisWidthRef.current)
      const insidePlot =
        ev.clientX >= g.plotLeft &&
        ev.clientX <= g.plotRight + (enableYRef.current ? yAxisWidthRef.current : 0) &&
        ev.clientY >= g.plotTop &&
        ev.clientY <= g.plotBottom + marginRef.current.bottom
      if (!insidePlot && !hoveredRef.current) return

      ev.preventDefault()
      ev.stopPropagation()

      // Normalize delta (pixels).
      let dy = ev.deltaY
      if (ev.deltaMode === 1) dy *= 16
      else if (ev.deltaMode === 2) dy *= 400

      const factor = Math.exp(dy * 0.0018)

      if (ev.shiftKey && enableYRef.current && setYRef.current) {
        const yFrac = clientToYFrac(ev.clientY, g)
        const next = zoomYAround(
          yDomainRef.current,
          factor,
          yFrac,
          minYSpanRef.current,
          clampYRef.current,
        )
        setYRef.current(next)
        return
      }

      const xFrac = clientToXFrac(ev.clientX, g)
      onXChangeRef.current(
        zoomXAround(xDomainRef.current, xFullRef.current, factor, xFrac),
      )
    }

    const onEnter = () => {
      hoveredRef.current = true
    }
    const onLeave = () => {
      hoveredRef.current = false
    }

    el.addEventListener('wheel', onWheel, { passive: false })
    el.addEventListener('pointerenter', onEnter)
    el.addEventListener('pointerleave', onLeave)
    return () => {
      el.removeEventListener('wheel', onWheel)
      el.removeEventListener('pointerenter', onEnter)
      el.removeEventListener('pointerleave', onLeave)
    }
  }, [])

  const xZoomed = !domainEqual(xDomain, xDefaultDomain)
  const yZoomed = Boolean(yManual)
  const canReset = xZoomed || yZoomed

  const bind: ChartViewportBind = {
    ref: wrapRef,
    tabIndex: -1,
    onPointerDown,
    onPointerMove,
    onPointerUp,
    onPointerCancel: onPointerUp,
    onPointerLeave,
    onDoubleClick: resetZoom,
    title: enableY
      ? 'Scroll to zoom time · Shift+scroll to zoom price · drag plot to pan · drag axes to scale · double-click / Reset to restore'
      : 'Scroll to zoom time · drag plot to pan · drag time axis to scale · double-click / Reset to restore',
  }

  return {
    wrapRef,
    hoverZone,
    dragging,
    xZoomed,
    yZoomed,
    canReset,
    resetZoom,
    bind,
    setHoverZone,
  }
}
