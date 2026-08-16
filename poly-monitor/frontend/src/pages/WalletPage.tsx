import { useEffect, useMemo, useRef, useState, type MouseEvent } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import {
  Area,
  Bar,
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import {
  api,
  formatCents,
  formatUsd,
  type MarketDetail,
  type SavedWalletRow,
  type WalletDailyRow,
  type WalletMarketActivity,
  type WalletMarketPnl,
  type WalletPnlInterval,
  type WalletPnlResponse,
  type WalletSummary,
  type WalletTotalPnlInterval,
  type WalletTotalPnlResponse,
} from '../api'
import PriceChart, {
  type BtcSeriesVisibility,
  type TimeDomain,
  type TraderMark,
} from '../components/PriceChart'

const INTERVALS: { id: WalletPnlInterval; label: string; subtitle: string }[] = [
  { id: '1d', label: '1D', subtitle: 'Past Day' },
  { id: '1w', label: '1W', subtitle: 'Past Week' },
  { id: '1m', label: '1M', subtitle: 'Past Month' },
  { id: '1y', label: '1Y', subtitle: 'Past Year' },
  { id: 'ytd', label: 'YTD', subtitle: 'Year to Date' },
  { id: 'all', label: 'ALL', subtitle: 'All Time' },
]

const TOTAL_PNL_INTERVALS: { id: WalletTotalPnlInterval; label: string }[] = [
  { id: '1d', label: '1D' },
  { id: '1w', label: '1W' },
  { id: '1m', label: '1M' },
  { id: 'all', label: 'ALL' },
]

const TOTAL_PNL_COLORS = {
  pnl: '#0f9d8a',
  fee: '#f59e0b',
  reward: '#8b5cf6',
  deposit: '#3b82f6',
  withdraw: '#ef4444',
} as const

const ADDR_RE = /^0x[a-fA-F0-9]{40}$/
const BTC_SLUG_RE = /^btc-updown-5m-(\d+)$/i
const DEFAULT_BTC_SERIES: BtcSeriesVisibility = { twap: true, chainlink: true, binance: true }

function slugToWindowStartMs(slug?: string | null): number | null {
  if (!slug) return null
  const m = BTC_SLUG_RE.exec(slug.trim())
  if (!m) return null
  const n = Number(m[1])
  if (!Number.isFinite(n) || n <= 0) return null
  return n > 1e12 ? n : n * 1000
}

/** Official BTC 5m market window from slug (ET wall-clock range). */
function slugMarketWindow(slug?: string | null): { startMs: number; endMs: number } | null {
  const startMs = slugToWindowStartMs(slug)
  if (startMs == null) return null
  return { startMs, endMs: startMs + 5 * 60_000 }
}

function isAfterMarketWindow(
  ts: number,
  window: { startMs: number; endMs: number } | null,
  graceMs = 60_000,
): boolean {
  if (!window || !Number.isFinite(ts)) return false
  return ts >= window.endMs + graceMs
}

function shorten(addr: string): string {
  const s = addr.trim()
  if (s.length <= 12) return s
  return `${s.slice(0, 6)}…${s.slice(-4)}`
}

function displayWalletName(name?: string | null, wallet?: string | null): string {
  const n = (name || '').trim()
  const w = (wallet || '').trim()
  if (!n) return w ? shorten(w) : '—'
  // Backend sometimes falls back to the full address as the "name".
  if (ADDR_RE.test(n) || (w && n.toLowerCase() === w.toLowerCase())) {
    return shorten(n)
  }
  return n
}

/** Two-letter avatar monogram; skips leading 0x on addresses. */
function avatarInitials(name?: string | null, wallet?: string | null): string {
  const n = (name || '').trim()
  const w = (wallet || '').trim()
  const raw =
    n && !(ADDR_RE.test(n) || (w && n.toLowerCase() === w.toLowerCase())) ? n : w || n
  if (!raw) return '?'
  const stripped = raw.replace(/^0x/i, '')
  const letters = stripped.replace(/[^a-zA-Z0-9]/g, '')
  if (letters.length >= 2) return letters.slice(0, 2).toUpperCase()
  if (letters.length === 1) return letters.toUpperCase()
  return '?'
}

/** Stable avatar colors derived from name/address text. */
function avatarColorFromName(name?: string | null, wallet?: string | null): string {
  const n = (name || '').trim()
  const w = (wallet || '').trim()
  const seed =
    (n && !(ADDR_RE.test(n) || (w && n.toLowerCase() === w.toLowerCase())) ? n : w || n) ||
    '?'
  let hash = 0
  for (let i = 0; i < seed.length; i++) {
    hash = (hash * 31 + seed.charCodeAt(i)) >>> 0
  }
  const hue = hash % 360
  const sat = 52 + (hash % 18) // 52–69%
  const light = 42 + ((hash >>> 8) % 12) // 42–53%
  return `hsl(${hue} ${sat}% ${light}%)`
}

function marketIdentityKey(m: {
  condition_id?: string | null
  slug?: string | null
  title?: string | null
}): string {
  return m.condition_id || m.slug || m.title || 'unknown'
}

function marketIdentityIds(m: {
  condition_id?: string | null
  slug?: string | null
  title?: string | null
} | null | undefined): Set<string> {
  const out = new Set<string>()
  if (!m) return out
  for (const v of [m.condition_id, m.slug, m.title]) {
    const s = (v || '').trim().toLowerCase()
    if (s) out.add(s)
  }
  return out
}

function findActivityMarket(
  markets: WalletMarketActivity[],
  expandedKey: string | null,
  meta?: {
    condition_id?: string | null
    slug?: string | null
    title?: string | null
  } | null,
): WalletMarketActivity | null {
  const ids = marketIdentityIds(meta)
  if (expandedKey) ids.add(expandedKey.trim().toLowerCase())
  if (!ids.size) return null
  for (const m of markets) {
    const candidates = [
      m.condition_id,
      m.slug,
      m.title,
      marketIdentityKey(m),
    ]
      .filter(Boolean)
      .map((s) => String(s).trim().toLowerCase())
    if (candidates.some((c) => ids.has(c))) return m
  }
  return null
}

function fmtSignedUsd(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return '—'
  const sign = n > 0 ? '+' : n < 0 ? '−' : ''
  return `${sign}$${formatUsd(Math.abs(n))}`
}

function fmtTimeShort(ms: number): string {
  try {
    return new Intl.DateTimeFormat('en-US', {
      timeZone: 'America/New_York',
      hour: 'numeric',
      minute: '2-digit',
      second: '2-digit',
      hour12: true,
    }).format(new Date(ms))
  } catch {
    return new Date(ms).toLocaleTimeString()
  }
}

function fmtTimeHm(ms: number): string {
  try {
    return new Intl.DateTimeFormat('en-US', {
      timeZone: 'America/New_York',
      hour: 'numeric',
      minute: '2-digit',
      hour12: true,
    })
      .format(new Date(ms))
      .replace(/\s+/g, '')
  } catch {
    return ''
  }
}

function formatCompactUsd(n: number): string {
  if (!Number.isFinite(n)) return '—'
  const abs = Math.abs(n)
  if (abs >= 1000) return `${n < 0 ? '−' : ''}${(abs / 1000).toFixed(1)}k`
  return formatUsd(n)
}

/** Buy spend, redeem proceeds, and net from a market's activity tape. */
function computeWasteEarn(rows: {
  type?: string
  side?: string | null
  shares?: number
  usd?: number
}[]) {
  let buyUsd = 0
  let buyShares = 0
  let sellUsd = 0
  let sellShares = 0
  let redeemUsd = 0
  let redeemShares = 0
  for (const row of rows) {
    const typ = (row.type || '').toUpperCase()
    const side = (row.side || '').toUpperCase()
    const shares = Number(row.shares) || 0
    const usd = Number(row.usd) || 0
    if (typ === 'REDEEM') {
      redeemShares += shares
      // Losing-side redeems often report usdcSize=0; do not treat shares as $1 earned.
      redeemUsd += Math.max(0, usd)
      continue
    }
    // SPLIT: USDC → equal Up + Down shares. MERGE: equal Up + Down → USDC.
    if (typ === 'SPLIT') {
      buyShares += shares * 2
      buyUsd += usd > 0 ? usd : shares
      continue
    }
    if (typ === 'MERGE') {
      sellShares += shares * 2
      sellUsd += usd > 0 ? usd : shares
      continue
    }
    if (side === 'SELL' || typ === 'SELL') {
      sellShares += shares
      sellUsd += usd
      continue
    }
    if (side === 'BUY' || typ === 'TRADE' || typ === 'BUY' || !side) {
      buyShares += shares
      buyUsd += usd
    }
  }
  // Waste = capital spent buying Up/Down tokens (incl. SPLIT).
  // Earned = money returned via sells + redeems (incl. MERGE).
  const wastedShares = buyShares
  const wastedMoney = buyUsd
  const earnedMoney = sellUsd + redeemUsd
  const profitMoney = earnedMoney - buyUsd
  return {
    buyUsd,
    buyShares,
    sellUsd,
    sellShares,
    redeemUsd,
    redeemShares,
    wastedShares,
    wastedMoney,
    earnedMoney,
    profitMoney,
  }
}

function fmtChartTick(ms: number, interval: WalletPnlInterval): string {
  try {
    if (interval === '1d') {
      return new Intl.DateTimeFormat('en-US', {
        timeZone: 'America/New_York',
        hour: 'numeric',
        minute: '2-digit',
        hour12: true,
      }).format(new Date(ms))
    }
    return new Intl.DateTimeFormat('en-US', {
      timeZone: 'America/New_York',
      month: 'short',
      day: 'numeric',
    }).format(new Date(ms))
  } catch {
    return ''
  }
}

function fmtChartTipTime(ms: number): string {
  try {
    return `${new Intl.DateTimeFormat('en-US', {
      timeZone: 'America/New_York',
      month: 'short',
      day: '2-digit',
      year: 'numeric',
      hour: 'numeric',
      minute: '2-digit',
      second: '2-digit',
      hour12: true,
    }).format(new Date(ms))} ET`
  } catch {
    return ''
  }
}

/** "Bitcoin Up or Down - August 12, 12:05PM-12:10PM ET" → "12:05PM–12:10PM" */
function shortMarketLabel(title?: string | null, slug?: string | null): string {
  const t = (title || '').trim()
  if (t) {
    const window = t.match(/(\d{1,2}:\d{2}\s*[AP]M)\s*-\s*(\d{1,2}:\d{2}\s*[AP]M)/i)
    if (window) {
      return `${window[1].replace(/\s+/g, '')}–${window[2].replace(/\s+/g, '')}`
    }
    const stripped = t.replace(/^Bitcoin\s+Up\s+or\s+Down\s*[-–—:]?\s*/i, '').trim()
    if (stripped) return stripped
  }
  const s = (slug || '').trim()
  if (/^btc-updown-5m-\d+$/i.test(s)) {
    const startSec = Number(s.slice('btc-updown-5m-'.length))
    if (Number.isFinite(startSec) && startSec > 0) {
      const start = new Date(startSec * 1000)
      const end = new Date((startSec + 300) * 1000)
      const fmt = (d: Date) =>
        d
          .toLocaleTimeString('en-US', {
            timeZone: 'America/New_York',
            hour: 'numeric',
            minute: '2-digit',
            hour12: true,
          })
          .replace(/\s+/g, '')
      return `${fmt(start)}–${fmt(end)}`
    }
  }
  return t || s || '—'
}

export default function WalletPage() {
  const { walletAddress: walletParam } = useParams<{ walletAddress?: string }>()
  const navigate = useNavigate()
  const [query, setQuery] = useState(walletParam || '')
  const [wallet, setWallet] = useState<string | null>(null)
  const [date, setDate] = useState<string>('')
  const [interval, setInterval] = useState<WalletPnlInterval>('1d')
  const [totalPnlInterval, setTotalPnlInterval] = useState<WalletTotalPnlInterval>('1w')
  const loadGen = useRef(0)

  const [summary, setSummary] = useState<WalletSummary | null>(null)
  const [pnl, setPnl] = useState<WalletPnlResponse | null>(null)
  const [totalPnl, setTotalPnl] = useState<WalletTotalPnlResponse | null>(null)
  const [daily, setDaily] = useState<WalletDailyRow[]>([])
  const [, setDailyHasMore] = useState(false)
  const [dailyScanLimit, setDailyScanLimit] = useState(3000)
  const [activityMarkets, setActivityMarkets] = useState<WalletMarketActivity[]>([])
  const [, setActivityNextOffset] = useState(0)
  const [, setActivityHasMore] = useState(false)
  const [activityLimit, setActivityLimit] = useState(500)
  const [marketPnls, setMarketPnls] = useState<WalletMarketPnl[]>([])
  const [marketsTotalPnl, setMarketsTotalPnl] = useState<number | null>(null)
  /** Date that marketsTotalPnl was computed for — prevents stamping stale totals. */
  const marketsTotalPnlDateRef = useRef<string | null>(null)
  const [, setMarketsHasMore] = useState(false)
  const [marketsLimit, setMarketsLimit] = useState(200)
  const [expandedMarket, setExpandedMarket] = useState<string | null>(null)
  const [savedWallets, setSavedWallets] = useState<SavedWalletRow[]>([])
  const [fromCache, setFromCache] = useState(false)

  const [loading, setLoading] = useState(false)
  const [pnlLoading, setPnlLoading] = useState(false)
  const [totalPnlLoading, setTotalPnlLoading] = useState(false)
  const [activityLoading, setActivityLoading] = useState(false)
  const [dailyMoreLoading, setDailyMoreLoading] = useState(false)
  const [marketsMoreLoading, setMarketsMoreLoading] = useState(false)
  const [activityMoreLoading, setActivityMoreLoading] = useState(false)
  const [marketFillLoading, setMarketFillLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const [marketDetail, setMarketDetail] = useState<MarketDetail | null>(null)
  const [chartLoading, setChartLoading] = useState(false)
  const [chartError, setChartError] = useState<string | null>(null)
  const [btcSeriesVisible, setBtcSeriesVisible] =
    useState<BtcSeriesVisibility>(DEFAULT_BTC_SERIES)
  const [sharedXDomain, setSharedXDomain] = useState<TimeDomain | null>(null)
  const [sharedHoverTime, setSharedHoverTime] = useState<number | null>(null)
  const [activityHighlightTs, setActivityHighlightTs] = useState<number | null>(null)
  const [copiedAddr, setCopiedAddr] = useState(false)
  const [commentDraft, setCommentDraft] = useState('')
  const [commentSaving, setCommentSaving] = useState(false)
  const [commentSavedAt, setCommentSavedAt] = useState<number | null>(null)
  const commentTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const commentWalletRef = useRef<string | null>(null)
  const slugEnrichDoneRef = useRef<Set<string>>(new Set())

  const [deleteConfirm, setDeleteConfirm] = useState<SavedWalletRow | null>(null)

  const refreshSavedList = async () => {
    try {
      const res = await api.savedWallets()
      setSavedWallets(res.wallets || [])
    } catch {
      /* ignore list errors */
    }
  }

  useEffect(() => {
    void refreshSavedList()
  }, [])

  const clearMarketsPnl = () => {
    marketsTotalPnlDateRef.current = null
    setMarketsTotalPnl(null)
    setMarketPnls([])
  }

  const applyMarketsPnl = (
    forDate: string,
    total: number | null,
    markets: WalletMarketPnl[],
  ) => {
    marketsTotalPnlDateRef.current = forDate
    setMarketPnls(markets)
    setMarketsTotalPnl(total)
  }

  const selectDailyDate = (next: string) => {
    if (next === date) return
    // Clear synchronously so the next render never shows the previous day's total.
    setActivityLoading(true)
    clearMarketsPnl()
    setActivityMarkets([])
    setExpandedMarket(null)
    setActivityHighlightTs(null)
    setDate(next)
  }

  const loadWalletData = async (raw: string, opts?: { refresh?: boolean }) => {
    const addr = raw.trim()
    if (!ADDR_RE.test(addr)) {
      setError('Enter a valid wallet address (0x + 40 hex chars)')
      return
    }
    const normalized = addr.toLowerCase()
    const refresh = Boolean(opts?.refresh)
    const gen = ++loadGen.current

    // Clear day-scoped state first so we never live-fetch the previous wallet's date
    // against this address (that cache-miss path is what made "cached" switches slow).
    setLoading(true)
    setError(null)
    setWallet(normalized)
    setQuery(normalized)
    setDate('')
    setDaily([])
    setDailyHasMore(false)
    setActivityMarkets([])
    clearMarketsPnl()
    setExpandedMarket(null)
    setMarketDetail(null)
    setChartError(null)
    setActivityHighlightTs(null)
    setPnl(null)
    setTotalPnl(null)
    setDailyScanLimit(3000)
    setActivityLimit(500)
    setMarketsLimit(200)
    setCommentSavedAt(null)
    commentWalletRef.current = normalized

    // Instant profile paint from the saved-wallet list while cache/API loads.
    // Leave total_pnl empty — saved list can be stale vs markets-aligned totals.
    const savedHit = savedWallets.find((w) => w.wallet === normalized)
    if (savedHit && !refresh) {
      setCommentDraft(savedHit.comment || '')
      setSummary({
        wallet: normalized,
        name: savedHit.name || shorten(normalized),
        profile_image: savedHit.profile_image ?? null,
        positions_value: Number(savedHit.positions_value ?? 0),
        total_pnl: null,
        biggest_win: null,
        open_positions: 0,
        closed_sample: 0,
        polygonscan_url: `https://polygonscan.com/address/${normalized}`,
        orbscan_url: `https://orbscan.com/profile/${normalized}`,
        polymarket_url: `https://polymarket.com/profile/${normalized}`,
        comment: savedHit.comment || '',
      })
      setFromCache(true)
    } else {
      setCommentDraft('')
    }

    try {
      const [sum, dailyRes] = await Promise.all([
        api.walletSummary(normalized, { refresh }),
        api.walletDaily(normalized, 120, { refresh, scanLimit: 3000 }),
      ])
      if (gen !== loadGen.current) return
      setSummary(sum)
      setCommentDraft(sum.comment || '')
      setFromCache(Boolean(sum.cached || dailyRes.cached))
      const days = dailyRes.daily || []
      setDaily(days)
      setDailyHasMore(Boolean(dailyRes.has_more))
      if (dailyRes.scan_limit) setDailyScanLimit(dailyRes.scan_limit)
      if (days.length) {
        setDate(days[0].date)
      }
      void refreshSavedList()
    } catch (e) {
      if (gen !== loadGen.current) return
      setSummary(null)
      setDaily([])
      setDailyHasMore(false)
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      if (gen === loadGen.current) setLoading(false)
    }
  }

  const goToWallet = (raw: string) => {
    const addr = raw.trim()
    if (!ADDR_RE.test(addr)) {
      setError('Enter a valid wallet address (0x + 40 hex chars)')
      return
    }
    const normalized = addr.toLowerCase()
    if (walletParam?.toLowerCase() === normalized) {
      void loadWalletData(normalized)
      return
    }
    navigate(`/wallet/${normalized}`)
  }

  const removeSaved = async (addr: string, e: MouseEvent) => {
    e.preventDefault()
    e.stopPropagation()
    const row = savedWallets.find((w) => w.wallet === addr.toLowerCase()) || {
      wallet: addr.toLowerCase(),
      name: addr.toLowerCase(),
    }
    setDeleteConfirm(row)
  }

  const cancelDeleteSaved = () => setDeleteConfirm(null)

  const confirmDeleteSaved = async () => {
    const addr = deleteConfirm?.wallet
    if (!addr) return
    setDeleteConfirm(null)
    try {
      await api.deleteSavedWallet(addr)
      setSavedWallets((prev) => prev.filter((w) => w.wallet !== addr.toLowerCase()))
      if (wallet === addr.toLowerCase()) {
        navigate('/wallet')
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }

  const persistComment = async (addr: string, text: string) => {
    setCommentSaving(true)
    try {
      const res = await api.saveWalletComment(addr, text)
      if (commentWalletRef.current !== addr) return
      const saved = res.comment || ''
      setCommentDraft(saved)
      setCommentSavedAt(Date.now())
      setSummary((prev) => (prev && prev.wallet === addr ? { ...prev, comment: saved } : prev))
      setSavedWallets((prev) => {
        const hit = prev.find((w) => w.wallet === addr)
        if (hit) {
          return prev.map((w) => (w.wallet === addr ? { ...w, comment: saved } : w))
        }
        return prev
      })
      void refreshSavedList()
    } catch (err) {
      if (commentWalletRef.current === addr) {
        setError(err instanceof Error ? err.message : String(err))
      }
    } finally {
      if (commentWalletRef.current === addr) setCommentSaving(false)
    }
  }

  const onCommentChange = (value: string) => {
    setCommentDraft(value)
    setCommentSavedAt(null)
    const addr = commentWalletRef.current || wallet
    if (!addr) return
    if (commentTimer.current) clearTimeout(commentTimer.current)
    commentTimer.current = setTimeout(() => {
      void persistComment(addr, value)
    }, 500)
  }

  useEffect(() => {
    return () => {
      if (commentTimer.current) clearTimeout(commentTimer.current)
    }
  }, [])

  const hardRefresh = async () => {
    if (!wallet) {
      goToWallet(query)
      return
    }
    const addr = wallet
    const scan = 3000
    const actLim = 500
    const mktLim = 200
    setDailyScanLimit(scan)
    setActivityLimit(actLim)
    setMarketsLimit(mktLim)
    setLoading(true)
    setPnlLoading(true)
    setTotalPnlLoading(true)
    setActivityLoading(true)
    setError(null)
    try {
      const [sum, dailyRes, pnlRes, totalPnlRes, act, mkts] = await Promise.all([
        api.walletSummary(addr, { refresh: true }),
        api.walletDaily(addr, 120, { refresh: true, scanLimit: scan }),
        api.walletPnl(addr, interval, { refresh: true }),
        api.walletTotalPnl(addr, totalPnlInterval),
        api.walletActivity(addr, { date, limit: actLim, refresh: true }),
        api.walletMarkets(addr, {
          date,
          limit: mktLim,
          activityLimit: actLim,
          refresh: true,
        }),
      ])
      setSummary(sum)
      setFromCache(false)
      const days = dailyRes.daily || []
      setDaily(days)
      setDailyHasMore(Boolean(dailyRes.has_more))
      setPnl(pnlRes)
      setTotalPnl(totalPnlRes)
      setActivityMarkets(act.markets || [])
      setActivityNextOffset(act.next_offset ?? act.count ?? 0)
      setActivityHasMore(Boolean(act.has_more))
      applyMarketsPnl(date, mkts.total_pnl ?? null, mkts.markets || [])
      setMarketsHasMore(Boolean(mkts.has_more))
      const firstKey =
        (mkts.markets?.[0]?.condition_id ||
          mkts.markets?.[0]?.slug ||
          mkts.markets?.[0]?.title ||
          act.markets?.[0]?.condition_id ||
          act.markets?.[0]?.slug ||
          act.markets?.[0]?.title) ??
        null
      setExpandedMarket(firstKey)
      void refreshSavedList()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
      setPnlLoading(false)
      setTotalPnlLoading(false)
      setActivityLoading(false)
    }
  }

  const fetchMoreDaily = async () => {
    if (!wallet || dailyMoreLoading) return
    setDailyMoreLoading(true)
    setError(null)
    try {
      const oldest = daily.length ? daily[daily.length - 1].date : undefined
      const nextScan = Math.min(dailyScanLimit + 3000, 50000)
      // Deeper scan of the full history (rewrites list), then also pull a page before oldest.
      const [deep, older] = await Promise.all([
        api.walletDaily(wallet, 180, { refresh: true, scanLimit: nextScan }),
        oldest
          ? api.walletDaily(wallet, 90, { scanLimit: nextScan, before: oldest })
          : Promise.resolve(null),
      ])
      setDailyScanLimit(nextScan)
      const byDate = new Map<string, WalletDailyRow>()
      for (const row of [...(deep.daily || []), ...(older?.daily || []), ...daily]) {
        if (!byDate.has(row.date)) byDate.set(row.date, row)
      }
      const merged = Array.from(byDate.values()).sort((a, b) => (a.date < b.date ? 1 : -1))
      setDaily(merged)
      setDailyHasMore(Boolean(deep.has_more || older?.has_more))
      setFromCache(false)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setDailyMoreLoading(false)
    }
  }

  const fetchMoreMarkets = async () => {
    if (!wallet || !date || marketsMoreLoading) return
    setMarketsMoreLoading(true)
    setError(null)
    try {
      const nextLim = Math.min(marketsLimit + 150, 500)
      const nextAct = Math.min(activityLimit + 250, 1000)
      const mkts = await api.walletMarkets(wallet, {
        date,
        limit: nextLim,
        activityLimit: nextAct,
        refresh: true,
      })
      setMarketsLimit(nextLim)
      setActivityLimit(nextAct)
      applyMarketsPnl(date, mkts.total_pnl ?? null, mkts.markets || [])
      setMarketsHasMore(Boolean(mkts.has_more))
      setFromCache(false)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setMarketsMoreLoading(false)
    }
  }

  const fetchMoreActivity = async () => {
    if (!wallet || !date || activityMoreLoading) return
    setActivityMoreLoading(true)
    setError(null)
    try {
      const selectedKey = expandedMarket
      const selectedMeta =
        marketPnls.find((m) => marketIdentityKey(m) === selectedKey) ?? null
      const beforeCount =
        findActivityMarket(activityMarkets, selectedKey, selectedMeta)?.activity.length ?? 0

      // Prefer deepening from offset 0 with a higher BTC limit so the day cache is
      // rewritten completely (fixes stale partial market tapes). Then keep paging
      // with the real API offset until the selected market gains rows or we exhaust.
      const deepLimit = Math.min(Math.max(activityLimit + 500, 1000), 1000)
      let page = await api.walletActivity(wallet, {
        date,
        limit: deepLimit,
        offset: 0,
        refresh: true,
      })
      let markets = page.markets || []
      let nextOffset = page.next_offset ?? page.count ?? 0
      let hasMore = Boolean(page.has_more)

      const countForSelected = (list: typeof markets) =>
        findActivityMarket(list, selectedKey, selectedMeta)?.activity.length ?? 0

      let guard = 0
      while (
        selectedKey &&
        countForSelected(markets) <= beforeCount &&
        hasMore &&
        guard < 6
      ) {
        guard += 1
        const more = await api.walletActivity(wallet, {
          date,
          limit: 400,
          offset: nextOffset,
          refresh: true,
        })
        const incoming = more.markets || []
        const byKey = new Map<string, WalletMarketActivity>()
        for (const m of markets) {
          byKey.set(marketIdentityKey(m), m)
        }
        for (const m of incoming) {
          const key = marketIdentityKey(m)
          const cur = byKey.get(key)
          if (!cur) {
            byKey.set(key, m)
            continue
          }
          const seen = new Set(
            (cur.activity || []).map((a) => `${a.timestamp}|${a.transaction_hash || ''}|${a.type}`),
          )
          const extra = (m.activity || []).filter(
            (a) => !seen.has(`${a.timestamp}|${a.transaction_hash || ''}|${a.type}`),
          )
          byKey.set(key, {
            ...cur,
            n_events: cur.n_events + extra.length,
            volume_usd:
              Number(cur.volume_usd || 0) + extra.reduce((s, a) => s + (a.usd || 0), 0),
            activity: [...(cur.activity || []), ...extra].sort(
              (a, b) => b.timestamp - a.timestamp,
            ),
            pnl: cur.pnl ?? m.pnl,
          })
        }
        markets = Array.from(byKey.values())
        nextOffset = more.next_offset ?? nextOffset + (more.count || 0)
        hasMore = Boolean(more.has_more)
      }

      // Closed PnL can land on settle day while fills happened in the prior
      // evening window — pull that market's slug window directly.
      if (selectedKey && countForSelected(markets) === 0 && selectedMeta?.slug) {
        const bySlug = await api.walletActivity(wallet, {
          slug: selectedMeta.slug,
          limit: 200,
          refresh: true,
        })
        for (const m of bySlug.markets || []) {
          const existing = findActivityMarket(markets, selectedKey, selectedMeta)
          if (!existing) {
            markets = [...markets, m]
            continue
          }
          const seen = new Set(
            (existing.activity || []).map(
              (a) => `${a.timestamp}|${a.transaction_hash || ''}|${a.type}`,
            ),
          )
          const extra = (m.activity || []).filter(
            (a) => !seen.has(`${a.timestamp}|${a.transaction_hash || ''}|${a.type}`),
          )
          if (!extra.length) continue
          existing.activity = [...existing.activity, ...extra].sort(
            (a, b) => b.timestamp - a.timestamp,
          )
          existing.n_events = existing.activity.length
          existing.volume_usd =
            Number(existing.volume_usd || 0) + extra.reduce((s, a) => s + (a.usd || 0), 0)
        }
      }

      setActivityLimit(deepLimit)
      setActivityMarkets(markets)
      setActivityNextOffset(nextOffset)
      setActivityHasMore(hasMore)
      setFromCache(false)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setActivityMoreLoading(false)
    }
  }

  useEffect(() => {
    if (!walletParam) {
      setWallet(null)
      setSummary(null)
      setDaily([])
      setDailyHasMore(false)
      setActivityMarkets([])
      clearMarketsPnl()
      setMarketsHasMore(false)
      setActivityHasMore(false)
      setPnl(null)
      setTotalPnl(null)
      setExpandedMarket(null)
      setFromCache(false)
      setError(null)
      setQuery('')
      return
    }
    if (!ADDR_RE.test(walletParam)) {
      setError('Invalid wallet address in URL')
      setWallet(null)
      return
    }
    void loadWalletData(walletParam)
    // eslint-disable-next-line react-hooks/exhaustive-deps -- reload when URL wallet changes
  }, [walletParam])

  useEffect(() => {
    if (!wallet) {
      setPnl(null)
      return
    }
    let cancelled = false
    setPnlLoading(true)
    api
      .walletPnl(wallet, interval)
      .then((res) => {
        if (!cancelled) {
          setPnl(res)
          if (res.cached) setFromCache(true)
        }
      })
      .catch((e) => {
        if (!cancelled) {
          setPnl(null)
          setError(e instanceof Error ? e.message : String(e))
        }
      })
      .finally(() => {
        if (!cancelled) setPnlLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [wallet, interval])

  useEffect(() => {
    if (!wallet) {
      setTotalPnl(null)
      return
    }
    let cancelled = false
    setTotalPnlLoading(true)
    api
      .walletTotalPnl(wallet, totalPnlInterval)
      .then((res) => {
        if (!cancelled) setTotalPnl(res)
      })
      .catch((e) => {
        if (!cancelled) {
          setTotalPnl(null)
          setError(e instanceof Error ? e.message : String(e))
        }
      })
      .finally(() => {
        if (!cancelled) setTotalPnlLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [wallet, totalPnlInterval])

  useEffect(() => {
    if (!wallet || !date) {
      setActivityMarkets([])
      clearMarketsPnl()
      setMarketsHasMore(false)
      setActivityHasMore(false)
      setActivityNextOffset(0)
      return
    }
    slugEnrichDoneRef.current = new Set()
    const loadDate = date
    let cancelled = false
    setActivityLoading(true)
    setExpandedMarket(null)
    setActivityHighlightTs(null)
    // Drop previous day's totals immediately so sync/UI never flash stale PnL.
    clearMarketsPnl()
    setActivityMarkets([])
    setMarketsHasMore(false)
    setActivityHasMore(false)
    setActivityNextOffset(0)
    Promise.all([
      api.walletActivity(wallet, { date: loadDate, limit: activityLimit }),
      api.walletMarkets(wallet, {
        date: loadDate,
        limit: marketsLimit,
        activityLimit,
      }),
    ])
      .then(([act, mkts]) => {
        if (cancelled) return
        setActivityMarkets(act.markets || [])
        setActivityNextOffset(act.next_offset ?? act.count ?? 0)
        setActivityHasMore(Boolean(act.has_more))
        applyMarketsPnl(loadDate, mkts.total_pnl ?? null, mkts.markets || [])
        setMarketsHasMore(Boolean(mkts.has_more))
        if (act.cached || mkts.cached) setFromCache(true)
        const firstKey =
          (mkts.markets?.[0]?.condition_id ||
            mkts.markets?.[0]?.slug ||
            mkts.markets?.[0]?.title ||
            act.markets?.[0]?.condition_id ||
            act.markets?.[0]?.slug ||
            act.markets?.[0]?.title) ??
          null
        setExpandedMarket(firstKey)
      })
      .catch((e) => {
        if (!cancelled) {
          setActivityMarkets([])
          clearMarketsPnl()
          setError(e instanceof Error ? e.message : String(e))
        }
      })
      .finally(() => {
        if (!cancelled) setActivityLoading(false)
      })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- limits bump via Fetch more handlers
  }, [wallet, date])

  // Daily API includes open marks; market panel may still refine via activity
  // tape (buy-only markets with no open row). Keep selected day in sync.
  // Only apply when marketsTotalPnl belongs to the currently selected date.
  useEffect(() => {
    if (!date || activityLoading || marketsTotalPnl == null) return
    if (marketsTotalPnlDateRef.current !== date) return
    setDaily((prev) => {
      let changed = false
      const next = prev.map((row) => {
        if (row.date !== date) return row
        const realized = row.realized_pnl ?? row.pnl
        if (row.pnl === marketsTotalPnl && row.realized_pnl === realized) return row
        changed = true
        return { ...row, realized_pnl: realized, pnl: marketsTotalPnl }
      })
      return changed ? next : prev
    })
  }, [date, marketsTotalPnl, activityLoading])

  const chartData = useMemo(() => {
    const series = pnl?.series || []
    if (!series.length) return []
    const base = series[0].pnl
    return series.map((p) => ({
      t: p.t,
      pnl: p.pnl,
      delta: p.pnl - base,
    }))
  }, [pnl])

  const totalPnlChartData = useMemo(() => totalPnl?.series || [], [totalPnl])
  const totalPnlValue = totalPnl?.pnl ?? null
  const totalPnlPositive = (totalPnlValue ?? 0) >= 0

  const selectedDayRebates = useMemo(() => {
    if (!date) return null
    const row = daily.find((d) => d.date === date)
    if (!row || row.rebates == null) return null
    return row.rebates
  }, [daily, date])

  const selectedMarketMeta = useMemo(() => {
    if (!expandedMarket) return null
    return (
      marketPnls.find((m) => marketIdentityKey(m) === expandedMarket) ?? null
    )
  }, [marketPnls, expandedMarket])

  const selectedMarketActivity = useMemo(
    () => findActivityMarket(activityMarkets, expandedMarket, selectedMarketMeta),
    [activityMarkets, expandedMarket, selectedMarketMeta],
  )

  // Day activity can miss post-midnight redeems (or only show the redeem on the
  // next day). Always merge the slug window tape once per selected market.
  useEffect(() => {
    if (!wallet || !expandedMarket || activityLoading || activityMoreLoading) return
    const slug = selectedMarketMeta?.slug
    if (!slug || !BTC_SLUG_RE.test(slug)) return
    if (slugEnrichDoneRef.current.has(slug)) return
    slugEnrichDoneRef.current.add(slug)
    let cancelled = false
    setMarketFillLoading(true)
    api
      .walletActivity(wallet, { slug, limit: 200, refresh: true })
      .then((res) => {
        if (cancelled) return
        const incoming = res.markets || []
        if (!incoming.length) return
        setActivityMarkets((prev) => {
          const next = [...prev]
          for (const m of incoming) {
            const idx = next.findIndex(
              (row) => findActivityMarket([row], expandedMarket, selectedMarketMeta) != null,
            )
            if (idx < 0) {
              next.push(m)
              continue
            }
            const hit = next[idx]
            const seen = new Set(
              (hit.activity || []).map(
                (a) => `${a.timestamp}|${a.transaction_hash || ''}|${a.type}`,
              ),
            )
            const extra = (m.activity || []).filter(
              (a) => !seen.has(`${a.timestamp}|${a.transaction_hash || ''}|${a.type}`),
            )
            if (!extra.length && (hit.activity || []).length >= (m.activity || []).length) {
              continue
            }
            const activity = (
              extra.length
                ? [...hit.activity, ...extra]
                : m.activity?.length
                  ? m.activity
                  : hit.activity
            ).sort((a, b) => b.timestamp - a.timestamp)
            next[idx] = {
              ...hit,
              activity,
              n_events: activity.length,
              volume_usd: activity.reduce((s, a) => s + (a.usd || 0), 0),
              pnl: m.pnl ?? hit.pnl,
            }
          }
          return next
        })
      })
      .catch(() => {
        /* keep day tape if slug lookup fails */
        slugEnrichDoneRef.current.delete(slug)
      })
      .finally(() => {
        if (!cancelled) setMarketFillLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [
    wallet,
    expandedMarket,
    selectedMarketMeta,
    activityLoading,
    activityMoreLoading,
  ])

  const sideFlow = useMemo(() => {
    const rows = selectedMarketActivity?.activity || []
    const empty = { shares: 0, usd: 0 }
    const out = {
      buyUp: { ...empty },
      buyDown: { ...empty },
      sellUp: { ...empty },
      sellDown: { ...empty },
    }
    for (const row of rows) {
      const typ = (row.type || '').toUpperCase()
      if (typ === 'REDEEM') continue
      const shares = Number(row.shares) || 0
      const usd = Number(row.usd) || 0
      // SPLIT mints equal Up + Down; MERGE burns equal Up + Down for USDC.
      if (typ === 'SPLIT') {
        const halfUsd = (usd > 0 ? usd : shares) / 2
        out.buyUp.shares += shares
        out.buyUp.usd += halfUsd
        out.buyDown.shares += shares
        out.buyDown.usd += halfUsd
        continue
      }
      if (typ === 'MERGE') {
        const halfUsd = (usd > 0 ? usd : shares) / 2
        out.sellUp.shares += shares
        out.sellUp.usd += halfUsd
        out.sellDown.shares += shares
        out.sellDown.usd += halfUsd
        continue
      }
      const side = (row.side || '').toUpperCase()
      const outcome = (row.outcome || '').toLowerCase()
      if (outcome !== 'up' && outcome !== 'down') continue
      const isSell = side === 'SELL' || typ === 'SELL'
      const isBuy = !isSell && (side === 'BUY' || typ === 'TRADE' || typ === 'BUY' || !side)
      if (!isBuy && !isSell) continue
      const bucket = isSell
        ? outcome === 'up'
          ? out.sellUp
          : out.sellDown
        : outcome === 'up'
          ? out.buyUp
          : out.buyDown
      bucket.shares += shares
      bucket.usd += usd
    }
    return out
  }, [selectedMarketActivity])

  const marketMoney = useMemo(
    () => computeWasteEarn(selectedMarketActivity?.activity || []),
    [selectedMarketActivity],
  )

  const showMarketMoneyBar =
    !!selectedMarketActivity &&
    (Math.abs(marketMoney.wastedMoney) > 0.005 ||
      Math.abs(marketMoney.earnedMoney) > 0.005 ||
      Math.abs(marketMoney.profitMoney) > 0.005)

  const showSideFlowBar =
    !!selectedMarketActivity &&
    (sideFlow.buyUp.shares > 0 ||
      sideFlow.buyDown.shares > 0 ||
      sideFlow.sellUp.shares > 0 ||
      sideFlow.sellDown.shares > 0)

  // Prefer activity slug (matches the Activity panel). PnL-row slug can be
  // missing/stale and previously drove the chart to a different 5m window.
  const selectedSlug =
    selectedMarketActivity?.slug || selectedMarketMeta?.slug || null

  const selectedMarketWindow = useMemo(
    () => slugMarketWindow(selectedSlug),
    [selectedSlug],
  )

  const traderMarks = useMemo((): TraderMark[] => {
    const rows = selectedMarketActivity?.activity || []
    return rows
      .filter((row) => {
        const typ = (row.type || '').toUpperCase()
        return !typ || typ === 'TRADE' || typ === 'BUY' || typ === 'SELL'
      })
      .map((row) => {
        const sideRaw = (row.side || '').toUpperCase()
        const side: 'BUY' | 'SELL' = sideRaw === 'SELL' ? 'SELL' : 'BUY'
        const outcomeRaw = (row.outcome || '').toLowerCase()
        const outcome: 'Up' | 'Down' = outcomeRaw === 'down' ? 'Down' : 'Up'
        const px = Number(row.price)
        // Activity API is usually 0–1; tolerate already-cents values.
        const pricePct = px > 1.5 ? px : px * 100
        return {
          t: Number(row.timestamp),
          pricePct,
          side,
          outcome,
        }
      })
      .filter((m) => Number.isFinite(m.t) && Number.isFinite(m.pricePct))
  }, [selectedMarketActivity])

  useEffect(() => {
    if (!selectedSlug) {
      setMarketDetail(null)
      setChartError(null)
      setSharedXDomain(null)
      setSharedHoverTime(null)
      setChartLoading(false)
      return
    }
    let cancelled = false
    const slug = selectedSlug
    const expectStart = slugToWindowStartMs(slug)
    setChartLoading(true)
    setChartError(null)
    setSharedHoverTime(null)
    // Drop previous market's series/domain immediately so the timeline never sticks.
    setMarketDetail(null)
    setSharedXDomain(null)

    ;(async () => {
      try {
        let marketId: string | null = null
        if (expectStart != null) {
          try {
            const hit = await api.marketAt('twap', { t: expectStart + 15_000 })
            // Reject nearest-miss / wrong-day hits: must be this slug's 5m window.
            const hitStart = Number(hit.start_time)
            const hitEnd = Number(hit.end_time)
            const inWindow =
              Number.isFinite(hitStart) &&
              Number.isFinite(hitEnd) &&
              hitStart <= expectStart + 15_000 &&
              expectStart + 15_000 < hitEnd
            const sameOpen =
              Number.isFinite(hitStart) && Math.abs(hitStart - expectStart) <= 60_000
            if (inWindow || sameOpen) {
              marketId = hit.market_id
            }
          } catch {
            /* fall through — no local series for this window */
          }
        }
        if (!marketId) {
          if (!cancelled) {
            setMarketDetail(null)
            setSharedXDomain(null)
            setChartError('No local price history for this market window')
          }
          return
        }
        const detail = await api.market(marketId, 'twap')
        if (cancelled) return
        // Final guard: detail window must still match the selected slug.
        if (
          expectStart != null &&
          detail.start_time != null &&
          Math.abs(Number(detail.start_time) - expectStart) > 60_000
        ) {
          setMarketDetail(null)
          setSharedXDomain(null)
          setChartError('No local price history for this market window')
          return
        }
        setMarketDetail(detail)
        const series = detail.series || []
        let domain: TimeDomain | null = null
        // Default view: official 5m window only (premarket reachable via pan/zoom-out).
        if (detail.start_time != null && detail.end_time != null) {
          domain = [detail.start_time, detail.end_time]
        } else if (series.length) {
          domain = [series[0].t, series[series.length - 1].t]
        }
        setSharedXDomain(domain)
      } catch (e) {
        if (!cancelled) {
          setMarketDetail(null)
          setSharedXDomain(null)
          setChartError(e instanceof Error ? e.message : String(e))
        }
      } finally {
        if (!cancelled) setChartLoading(false)
      }
    })()

    return () => {
      cancelled = true
    }
  }, [selectedSlug, expandedMarket])

  const priceChartData = useMemo(
    () =>
      (marketDetail?.series || []).map((p) => ({
        t: p.t,
        btc: p.btc,
        twap: p.twap,
        chainlink: p.chainlink,
        up: p.up,
        down: p.down,
      })),
    [marketDetail],
  )

  const xFullDomain = useMemo((): TimeDomain => {
    // Max zoom-out: market window plus any premarket samples.
    if (marketDetail?.start_time != null && marketDetail?.end_time != null) {
      let left = marketDetail.start_time
      for (const p of priceChartData) {
        const t = Number(p.t)
        if (Number.isFinite(t) && t < left) left = t
      }
      return [left, marketDetail.end_time]
    }
    if (priceChartData.length >= 2) {
      return [priceChartData[0].t, priceChartData[priceChartData.length - 1].t]
    }
    if (traderMarks.length >= 2) {
      const times = traderMarks.map((m) => m.t)
      return [Math.min(...times) - 5_000, Math.max(...times) + 5_000]
    }
    const now = Date.now()
    return [now - 300_000, now]
  }, [marketDetail, priceChartData, traderMarks])

  // Default view: full 5m market only. Premarket is reachable via zoom-out / pan.
  const xDefaultDomain = useMemo((): TimeDomain => {
    if (marketDetail?.start_time != null && marketDetail?.end_time != null) {
      const m0 = marketDetail.start_time
      const m1 = marketDetail.end_time
      if (m1 > m0) return [m0, m1]
      return [m0, m0 + 5 * 60_000]
    }
    const slugWin = selectedMarketWindow
    if (slugWin) return [slugWin.startMs, slugWin.endMs]
    const [f0, f1] = xFullDomain
    const end = Number.isFinite(f1) && f1 > f0 ? f1 : f0 + 5 * 60_000
    const start = Math.max(f0, end - 5 * 60_000)
    return [start, end]
  }, [marketDetail, selectedMarketWindow, xFullDomain])

  // Always prefer an explicit domain; fall back to the 5m market window.
  const chartXDomain = sharedXDomain ?? xDefaultDomain

  const chartMarketKey = marketDetail?.market_id || selectedSlug || 'none'

  const intervalMeta = INTERVALS.find((x) => x.id === interval) || INTERVALS[0]
  const pnlValue = pnl?.pnl ?? null
  const pnlPositive = (pnlValue ?? 0) >= 0

  const copyWalletAddress = async () => {
    const addr = summary?.wallet || wallet
    if (!addr) return
    try {
      await navigator.clipboard.writeText(addr)
      setCopiedAddr(true)
      window.setTimeout(() => setCopiedAddr(false), 1400)
    } catch {
      setError('Could not copy address')
    }
  }

  return (
    <div className="workspace wallet-workspace">
      <aside className="workspace-rail workspace-rail-left wallet-saved-rail">
        <div className="wallet-saved-panel">
          <div className="wallet-saved-heading">Saved wallets</div>
          <p className="wallet-saved-hint muted">
            Watched wallets are stored locally so reopen skips live fetch.
          </p>
          {savedWallets.length === 0 ? (
            <p className="muted wallet-saved-empty">No saved wallets yet.</p>
          ) : (
            <ul className="wallet-saved-list">
              {savedWallets.map((w) => {
                const active = wallet === w.wallet
                return (
                  <li key={w.wallet}>
                    <button
                      type="button"
                      className={`wallet-saved-item${active ? ' active' : ''}`}
                      onClick={() => goToWallet(w.wallet)}
                    >
                      <span
                        className="wallet-saved-avatar"
                        aria-hidden
                        style={
                          w.profile_image
                            ? undefined
                            : { background: avatarColorFromName(w.name, w.wallet) }
                        }
                      >
                        {w.profile_image ? (
                          <img src={w.profile_image} alt="" />
                        ) : (
                          avatarInitials(w.name, w.wallet)
                        )}
                      </span>
                      <span className="wallet-saved-meta">
                        <span className="wallet-saved-name">{w.name || shorten(w.wallet)}</span>
                        <span className="wallet-saved-addr">{shorten(w.wallet)}</span>
                        {w.comment?.trim() && (
                          <span className="wallet-saved-comment" title={w.comment}>
                            {w.comment.trim()}
                          </span>
                        )}
                        {w.total_pnl != null && (
                          <span className={`wallet-saved-pnl ${(w.total_pnl ?? 0) >= 0 ? 'up' : 'down'}`}>
                            {fmtSignedUsd(w.total_pnl)}
                          </span>
                        )}
                      </span>
                    </button>
                    <button
                      type="button"
                      className="wallet-saved-remove"
                      title="Remove saved wallet"
                      aria-label={`Remove ${w.name || w.wallet}`}
                      onClick={(e) => void removeSaved(w.wallet, e)}
                    >
                      ×
                    </button>
                  </li>
                )
              })}
            </ul>
          )}
        </div>
      </aside>

      <div className="workspace-main wallet-main">
        <div className="wallet-search-bar">
          <label className="wallet-search-field" htmlFor="wallet-search-input">
            <span className="wallet-search-icon" aria-hidden>
              <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
                <circle cx="7" cy="7" r="4.5" stroke="currentColor" strokeWidth="1.5" />
                <path d="M10.5 10.5L14 14" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
              </svg>
            </span>
            <span className="wallet-search-prefix">0x</span>
            <input
              id="wallet-search-input"
              type="text"
              spellCheck={false}
              autoComplete="off"
              autoCorrect="off"
              autoCapitalize="off"
              placeholder="Paste wallet address…"
              value={query.startsWith('0x') || query.startsWith('0X') ? query.slice(2) : query}
              disabled={loading}
              onChange={(e) => {
                const raw = e.target.value.trim()
                setQuery(raw ? (raw.startsWith('0x') || raw.startsWith('0X') ? raw : `0x${raw}`) : '')
              }}
              onKeyDown={(e) => {
                if (e.key === 'Enter') goToWallet(query)
              }}
            />
            {query && !loading && (
              <button
                type="button"
                className="wallet-search-clear"
                aria-label="Clear address"
                onClick={() => setQuery('')}
              >
                ×
              </button>
            )}
          </label>
          <div className="wallet-search-actions">
            <button
              type="button"
              className="wallet-search-btn primary"
              disabled={loading || !query.trim()}
              onClick={() => goToWallet(query)}
            >
              {loading ? 'Loading…' : 'Search'}
            </button>
            <button
              type="button"
              className="wallet-search-btn"
              disabled={loading || !wallet}
              title="Force live fetch and update local cache"
              onClick={() => void hardRefresh()}
            >
              Refresh
            </button>
            {fromCache && wallet && (
              <span className="wallet-cache-pill" title="Showing locally saved data">
                Cached
              </span>
            )}
          </div>
        </div>

        {error && <p className="error">{error}</p>}

        {!wallet && !error && (
          <p className="muted" style={{ marginTop: '0.25rem' }}>
            Search a wallet to load BTC Up/Down 5m PnL and activity. Results are saved locally for quick reopen.
          </p>
        )}

        {summary && (
          <section className="wallet-hero-row">
            <div className="wallet-profile-card">
              <div className="wallet-profile-top">
                <div
                  className="wallet-avatar"
                  aria-hidden
                  style={
                    summary.profile_image
                      ? undefined
                      : { background: avatarColorFromName(summary.name, summary.wallet) }
                  }
                >
                  {summary.profile_image ? (
                    <img src={summary.profile_image} alt="" />
                  ) : (
                    avatarInitials(summary.name, summary.wallet)
                  )}
                </div>
                <div className="wallet-profile-text">
                  <div
                    className="wallet-profile-name"
                    title={
                      ADDR_RE.test((summary.name || '').trim())
                        ? summary.wallet
                        : summary.name || summary.wallet
                    }
                  >
                    {displayWalletName(summary.name, summary.wallet)}
                  </div>
                  <div className="wallet-profile-addr-row">
                    <span className="wallet-profile-addr" title={summary.wallet}>
                      {shorten(summary.wallet)}
                    </span>
                    <button
                      type="button"
                      className="wallet-copy-btn"
                      onClick={() => void copyWalletAddress()}
                      title={copiedAddr ? 'Copied' : 'Copy address'}
                      aria-label={copiedAddr ? 'Address copied' : 'Copy wallet address'}
                    >
                      {copiedAddr ? 'Copied' : 'Copy'}
                    </button>
                  </div>
                </div>
              </div>
              <div className="wallet-profile-links">
                <a className="wallet-ext-link" href={summary.polymarket_url} target="_blank" rel="noreferrer">
                  Polymarket
                </a>
                <a className="wallet-ext-link" href={summary.orbscan_url} target="_blank" rel="noreferrer">
                  Orbscan
                </a>
                <a className="wallet-ext-link" href={summary.polygonscan_url} target="_blank" rel="noreferrer">
                  Polygonscan
                </a>
              </div>
              <div className="wallet-profile-stats">
                <div>
                  <div className="wallet-stat-value">${formatUsd(summary.positions_value)}</div>
                  <div className="wallet-stat-label">Positions Value</div>
                </div>
                <div>
                  <div className={`wallet-stat-value ${(summary.biggest_win?.realized_pnl ?? 0) >= 0 ? 'up' : 'down'}`}>
                    {summary.biggest_win
                      ? `$${formatUsd(summary.biggest_win.realized_pnl)}`
                      : '—'}
                  </div>
                  <div className="wallet-stat-label">Biggest Win</div>
                </div>
                <div>
                  <div className={`wallet-stat-value ${(summary.total_pnl ?? 0) >= 0 ? 'up' : 'down'}`}>
                    {summary.total_pnl != null ? fmtSignedUsd(summary.total_pnl) : '—'}
                  </div>
                  <div className="wallet-stat-label">All-time PnL</div>
                </div>
                <div title="Account-level maker + taker fee rebates (not BTC-only)">
                  <div className={`wallet-stat-value ${(summary.total_rebates ?? 0) >= 0 ? 'up' : 'down'}`}>
                    {summary.total_rebates != null ? fmtSignedUsd(summary.total_rebates) : '—'}
                  </div>
                  <div className="wallet-stat-label">Rebates</div>
                </div>
              </div>
              <div className="wallet-profile-scope muted">
                BTC Up/Down 5m · incl. unredeemed · rebates are account-wide
              </div>
              <div className="wallet-comment">
                <label className="wallet-comment-label" htmlFor="wallet-trader-comment">
                  Comment
                  <span className="wallet-comment-status muted">
                    {commentSaving ? 'Saving…' : commentSavedAt ? 'Saved' : ''}
                  </span>
                </label>
                <textarea
                  id="wallet-trader-comment"
                  className="wallet-comment-input"
                  rows={3}
                  maxLength={4000}
                  placeholder="Notes on this trader…"
                  value={commentDraft}
                  onChange={(e) => onCommentChange(e.target.value)}
                  onBlur={() => {
                    const addr = commentWalletRef.current || wallet
                    if (!addr) return
                    if (commentTimer.current) {
                      clearTimeout(commentTimer.current)
                      commentTimer.current = null
                    }
                    void persistComment(addr, commentDraft)
                  }}
                />
              </div>
            </div>

            <div className="wallet-pnl-card">
              <div className="wallet-pnl-header">
                <div className="wallet-pnl-title">
                  Profit/Loss
                  {pnlValue != null && (
                    <span className={`wallet-pnl-tri ${pnlPositive ? 'up' : 'down'}`} aria-hidden>
                      {pnlPositive ? '▲' : '▼'}
                    </span>
                  )}
                </div>
                <div className="wallet-interval-pills" role="group" aria-label="PnL interval">
                  {INTERVALS.map((opt) => (
                    <button
                      key={opt.id}
                      type="button"
                      className={`wallet-interval-pill${interval === opt.id ? ' active' : ''}`}
                      disabled={pnlLoading}
                      onClick={() => setInterval(opt.id)}
                    >
                      {opt.label}
                    </button>
                  ))}
                </div>
              </div>
              <div className={`wallet-pnl-value ${pnlPositive ? 'up' : 'down'}`}>
                {pnlLoading && !pnl ? '…' : fmtSignedUsd(pnlValue)}
              </div>
              <div className="wallet-pnl-sub">
                {intervalMeta.subtitle} · BTC Up/Down 5m
              </div>
              <div className="wallet-pnl-chart">
                {chartData.length > 1 ? (
                  <ResponsiveContainer width="100%" height={160}>
                    <ComposedChart data={chartData} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
                      <defs>
                        <linearGradient id="walletPnlFill" x1="0" y1="0" x2="0" y2="1">
                          <stop
                            offset="0%"
                            stopColor={pnlPositive ? 'var(--up)' : 'var(--down)'}
                            stopOpacity={0.28}
                          />
                          <stop
                            offset="100%"
                            stopColor={pnlPositive ? 'var(--up)' : 'var(--down)'}
                            stopOpacity={0.02}
                          />
                        </linearGradient>
                      </defs>
                      <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" vertical={false} />
                      <XAxis
                        dataKey="t"
                        type="number"
                        domain={['dataMin', 'dataMax']}
                        tickFormatter={(v) => fmtChartTick(Number(v), interval)}
                        tick={{ fontSize: 11, fill: 'var(--muted)' }}
                        minTickGap={40}
                      />
                      <YAxis
                        dataKey="delta"
                        width={56}
                        tick={{ fontSize: 11, fill: 'var(--muted)' }}
                        tickFormatter={(v) => fmtSignedUsd(Number(v))}
                      />
                      <Tooltip
                        contentStyle={{
                          background: 'var(--bg-panel)',
                          border: '1px solid var(--border)',
                          borderRadius: 8,
                          fontSize: 12,
                        }}
                        labelFormatter={(v) => fmtChartTipTime(Number(v))}
                        formatter={(value) => [fmtSignedUsd(Number(value ?? 0)), 'PnL']}
                      />
                      <Area
                        type="monotone"
                        dataKey="delta"
                        stroke="none"
                        fill="url(#walletPnlFill)"
                        isAnimationActive={false}
                      />
                      <Line
                        type="monotone"
                        dataKey="delta"
                        stroke={pnlPositive ? 'var(--up)' : 'var(--down)'}
                        strokeWidth={2}
                        dot={false}
                        isAnimationActive={false}
                      />
                    </ComposedChart>
                  </ResponsiveContainer>
                ) : (
                  <p className="muted" style={{ margin: '1.5rem 0', textAlign: 'center' }}>
                    {pnlLoading ? 'Loading chart…' : 'No PnL series for this interval'}
                  </p>
                )}
              </div>
            </div>
          </section>
        )}

        {wallet && (summary || totalPnl || totalPnlLoading) && (
          <section className="wallet-total-pnl-card">
            <div className="wallet-total-pnl-header">
              <div className="wallet-total-pnl-title-block">
                <div className="wallet-total-pnl-title">Total PnL</div>
                <div className={`wallet-total-pnl-value ${totalPnlPositive ? 'up' : 'down'}`}>
                  {totalPnlLoading && !totalPnl ? '…' : fmtSignedUsd(totalPnlValue)}
                </div>
                <div className="wallet-total-pnl-legend" aria-label="Chart legend">
                  <span>
                    <i style={{ background: TOTAL_PNL_COLORS.pnl }} className="wallet-total-pnl-swatch line" />
                    Total PnL
                  </span>
                  <span>
                    <i style={{ background: TOTAL_PNL_COLORS.fee }} className="wallet-total-pnl-swatch" />
                    Fee
                  </span>
                  <span>
                    <i style={{ background: TOTAL_PNL_COLORS.reward }} className="wallet-total-pnl-swatch" />
                    Reward
                  </span>
                  <span>
                    <i style={{ background: TOTAL_PNL_COLORS.deposit }} className="wallet-total-pnl-swatch" />
                    Deposit
                  </span>
                  <span>
                    <i style={{ background: TOTAL_PNL_COLORS.withdraw }} className="wallet-total-pnl-swatch" />
                    Withdraw
                  </span>
                </div>
              </div>
              <div className="wallet-interval-pills" role="group" aria-label="Total PnL interval">
                {TOTAL_PNL_INTERVALS.map((opt) => (
                  <button
                    key={opt.id}
                    type="button"
                    className={`wallet-interval-pill${totalPnlInterval === opt.id ? ' active' : ''}`}
                    disabled={totalPnlLoading}
                    onClick={() => setTotalPnlInterval(opt.id)}
                  >
                    {opt.label}
                  </button>
                ))}
              </div>
            </div>
            <div className="wallet-total-pnl-chart">
              {totalPnlChartData.length > 1 ? (
                <ResponsiveContainer width="100%" height={280}>
                  <ComposedChart
                    data={totalPnlChartData}
                    margin={{ top: 12, right: 12, left: 4, bottom: 4 }}
                  >
                    <defs>
                      <linearGradient id="walletTotalPnlFill" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopColor={TOTAL_PNL_COLORS.pnl} stopOpacity={0.22} />
                        <stop offset="100%" stopColor={TOTAL_PNL_COLORS.pnl} stopOpacity={0.02} />
                      </linearGradient>
                    </defs>
                    <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" vertical={false} />
                    <XAxis
                      dataKey="t"
                      type="number"
                      domain={['dataMin', 'dataMax']}
                      tickFormatter={(v) => fmtChartTick(Number(v), totalPnlInterval === 'all' ? 'all' : totalPnlInterval)}
                      tick={{ fontSize: 11, fill: 'var(--muted)' }}
                      minTickGap={48}
                    />
                    <YAxis
                      yAxisId="pnl"
                      width={56}
                      tick={{ fontSize: 11, fill: 'var(--muted)' }}
                      tickFormatter={(v) => {
                        const n = Number(v)
                        const abs = Math.abs(n)
                        if (abs >= 1000) return `${n < 0 ? '-' : ''}$${(abs / 1000).toFixed(abs >= 10000 ? 0 : 1)}K`
                        return fmtSignedUsd(n)
                      }}
                    />
                    <YAxis yAxisId="flow" orientation="right" hide />
                    <Tooltip
                      contentStyle={{
                        background: 'var(--bg-panel)',
                        border: '1px solid var(--border)',
                        borderRadius: 8,
                        fontSize: 12,
                      }}
                      labelFormatter={(v) => fmtChartTipTime(Number(v))}
                      formatter={(value, name) => {
                        const labels: Record<string, string> = {
                          pnl: 'Total PnL',
                          fee: 'Fee',
                          reward: 'Reward',
                          deposit: 'Deposit',
                          withdraw: 'Withdraw',
                        }
                        const key = String(name)
                        return [`$${formatUsd(Number(value ?? 0))}`, labels[key] || key]
                      }}
                    />
                    <Legend content={() => null} />
                    <Bar
                      yAxisId="flow"
                      dataKey="fee"
                      name="fee"
                      fill={TOTAL_PNL_COLORS.fee}
                      maxBarSize={10}
                      isAnimationActive={false}
                    />
                    <Bar
                      yAxisId="flow"
                      dataKey="reward"
                      name="reward"
                      fill={TOTAL_PNL_COLORS.reward}
                      maxBarSize={10}
                      isAnimationActive={false}
                    />
                    <Bar
                      yAxisId="flow"
                      dataKey="deposit"
                      name="deposit"
                      fill={TOTAL_PNL_COLORS.deposit}
                      maxBarSize={10}
                      isAnimationActive={false}
                    />
                    <Bar
                      yAxisId="flow"
                      dataKey="withdraw"
                      name="withdraw"
                      fill={TOTAL_PNL_COLORS.withdraw}
                      maxBarSize={10}
                      isAnimationActive={false}
                    />
                    <Area
                      yAxisId="pnl"
                      type="monotone"
                      dataKey="pnl"
                      name="pnl"
                      stroke="none"
                      fill="url(#walletTotalPnlFill)"
                      isAnimationActive={false}
                      legendType="none"
                      tooltipType="none"
                    />
                    <Line
                      yAxisId="pnl"
                      type="monotone"
                      dataKey="pnl"
                      name="pnl"
                      stroke={TOTAL_PNL_COLORS.pnl}
                      strokeWidth={2.25}
                      dot={false}
                      activeDot={{ r: 4, fill: TOTAL_PNL_COLORS.pnl }}
                      isAnimationActive={false}
                    />
                  </ComposedChart>
                </ResponsiveContainer>
              ) : (
                <p className="muted" style={{ margin: '2rem 0', textAlign: 'center' }}>
                  {totalPnlLoading ? 'Loading total PnL…' : 'No account PnL series for this interval'}
                </p>
              )}
            </div>
            <div className="wallet-total-pnl-foot muted">
              Account-wide · Reward = rebates and rewards
            </div>
          </section>
        )}

        {wallet && (
          <section className="wallet-split-row">
            <div className="wallet-panel">
              <div className="wallet-panel-head">
                <h2>PnL by day</h2>
                <span
                  className="muted"
                  title="Per-day total matches PnL by market (closed + unredeemed tape)."
                >
                  {daily.length} days
                </span>
              </div>
              <div className="wallet-daily-list">
                {daily.length === 0 && (
                  <p className="muted" style={{ padding: '0.75rem' }}>
                    {loading ? 'Loading…' : 'No daily PnL yet'}
                  </p>
                )}
                {daily.map((row) => {
                  const rebate = row.rebates ?? 0
                  const hasRebate = Math.abs(rebate) > 0.005
                  const tipParts = [
                    row.realized_pnl != null && row.realized_pnl !== row.pnl
                      ? `Includes unredeemed. Realized (closed only): ${fmtSignedUsd(row.realized_pnl)}`
                      : 'Closed settles; opens to full day total when markets load',
                    hasRebate
                      ? `Account rebates: ${fmtSignedUsd(rebate)}`
                      : null,
                  ].filter(Boolean)
                  return (
                  <button
                    key={row.date}
                    type="button"
                    className={`wallet-daily-row${date === row.date ? ' active' : ''}`}
                    onClick={() => selectDailyDate(row.date)}
                    title={tipParts.join(' · ')}
                  >
                    <span className="wallet-daily-date">{row.date}</span>
                    <span className="wallet-daily-nums">
                      <span className={`wallet-daily-pnl ${row.pnl >= 0 ? 'up' : 'down'}`}>
                        {fmtSignedUsd(row.pnl)}
                      </span>
                      {hasRebate ? (
                        <span className={`wallet-daily-rebate ${rebate >= 0 ? 'up' : 'down'}`}>
                          R {fmtSignedUsd(rebate)}
                        </span>
                      ) : null}
                    </span>
                  </button>
                  )
                })}
              </div>
              <div className="wallet-panel-foot">
                <button
                  type="button"
                  className="wallet-fetch-more"
                  disabled={dailyMoreLoading || loading}
                  onClick={() => void fetchMoreDaily()}
                >
                  {dailyMoreLoading ? 'Fetching…' : 'Fetch more'}
                </button>
              </div>
            </div>

            <div className="wallet-panel">
              <div className="wallet-panel-head">
                <h2>PnL by market · {date}</h2>
                <span
                  className="muted"
                  title="Total = closed settles + unredeemed/open tape estimates. Rebates are account-wide."
                >
                  {activityLoading
                    ? 'Loading…'
                    : `${marketPnls.length} mkts${
                        marketsTotalPnl != null ? ` · ${fmtSignedUsd(marketsTotalPnl)}` : ''
                      }${
                        selectedDayRebates != null && Math.abs(selectedDayRebates) > 0.005
                          ? ` · R ${fmtSignedUsd(selectedDayRebates)}`
                          : ''
                      }`}
                </span>
              </div>
              <div className="wallet-daily-list">
                {!activityLoading && marketPnls.length === 0 && (
                  <p className="muted" style={{ padding: '0.75rem' }}>
                    No market PnL on this date
                  </p>
                )}
                {marketPnls.map((m) => {
                  const key = marketIdentityKey(m)
                  return (
                    <button
                      key={key}
                      type="button"
                      className={`wallet-market-pnl-row${
                        expandedMarket === key ? ' active' : ''
                      }`}
                      onClick={() => setExpandedMarket(key)}
                      title={m.title || undefined}
                    >
                      <span className="wallet-market-pnl-title">
                        {shortMarketLabel(m.title, m.slug)}
                        {m.unredeemed && (
                          <span
                            className="wallet-unredeemed-pill"
                            title="Won shares still unclaimed (claimable value > $0)"
                          >
                            unredeemed
                          </span>
                        )}
                      </span>
                      <span
                        className={`wallet-daily-pnl ${(m.pnl ?? 0) >= 0 ? 'up' : 'down'}`}
                      >
                        {m.pnl != null ? fmtSignedUsd(m.pnl) : '—'}
                      </span>
                    </button>
                  )
                })}
              </div>
              <div className="wallet-panel-foot">
                <button
                  type="button"
                  className="wallet-fetch-more"
                  disabled={marketsMoreLoading || activityLoading || !date}
                  onClick={() => void fetchMoreMarkets()}
                >
                  {marketsMoreLoading ? 'Fetching…' : 'Fetch more'}
                </button>
              </div>
            </div>
          </section>
        )}

        {wallet && (
          <section className="wallet-split-row wallet-activity-charts-row">
            <div className="wallet-panel wallet-activity-panel">
              <div className="wallet-panel-head">
                <h2>Activity</h2>
                <div className="wallet-activity-head-stats">
                  {activityLoading ? (
                    <span className="muted">…</span>
                  ) : selectedMarketActivity ? (
                    <>
                      <span className="muted">
                        {selectedMarketActivity.n_events} · $
                        {formatCompactUsd(selectedMarketActivity.volume_usd)}
                      </span>
                      {marketMoney.wastedMoney > 0.005 && (
                        <span
                          className="wallet-money-pill waste"
                          title="Selected market: USD spent buying Up/Down tokens"
                        >
                          Waste ${formatCompactUsd(marketMoney.wastedMoney)}
                          {marketMoney.wastedShares > 0.005 && (
                            <span className="wallet-money-shares">
                              · {formatCompactUsd(marketMoney.wastedShares)} sh
                            </span>
                          )}
                        </span>
                      )}
                    </>
                  ) : (
                    <span className="muted">
                      {marketFillLoading ? 'Loading…' : expandedMarket ? 'None' : '—'}
                    </span>
                  )}
                </div>
              </div>
              {(selectedMarketMeta?.title ||
                selectedMarketMeta?.slug ||
                selectedMarketActivity?.title ||
                selectedMarketActivity?.slug) && (
                <div className="wallet-activity-selected-title">
                  <span className="wallet-activity-selected-left">
                    <span
                      title={
                        selectedMarketMeta?.title ||
                        selectedMarketActivity?.title ||
                        undefined
                      }
                    >
                      {shortMarketLabel(
                        selectedMarketMeta?.title || selectedMarketActivity?.title,
                        selectedMarketMeta?.slug || selectedMarketActivity?.slug,
                      )}
                    </span>
                    {selectedMarketMeta?.unredeemed && (
                      <span
                        className="wallet-market-window-hint wallet-unredeemed-hint"
                        title="Winning shares still unclaimed — claimable value is left on this market."
                      >
                        Unredeemed
                        {selectedMarketMeta.open_shares != null &&
                          selectedMarketMeta.open_shares > 0 && (
                            <>
                              {' '}
                              · {formatCompactUsd(selectedMarketMeta.open_shares)} sh
                              {selectedMarketMeta.open_value != null
                                ? ` · $${formatCompactUsd(selectedMarketMeta.open_value)}`
                                : ''}
                            </>
                          )}
                      </span>
                    )}
                    {selectedMarketWindow &&
                      selectedMarketActivity?.activity.some((row) =>
                        isAfterMarketWindow(Number(row.timestamp), selectedMarketWindow),
                      ) && (
                        <span
                          className="wallet-market-window-hint muted"
                          title={`Official market window ${fmtTimeHm(selectedMarketWindow.startMs)}–${fmtTimeHm(selectedMarketWindow.endMs)} ET. Redeems and late fills can land after the slot closes.`}
                        >
                          fills after {fmtTimeHm(selectedMarketWindow.endMs)} marked
                        </span>
                      )}
                  </span>
                  {showMarketMoneyBar ? (
                    <span
                      className="wallet-timeline-money"
                      title="This market: buy spend / sells+redeems / net profit"
                    >
                      <span className="down">
                        Waste ${formatCompactUsd(marketMoney.wastedMoney)}
                        {marketMoney.wastedShares > 0.005 && (
                          <span className="muted">
                            {' '}
                            ({formatCompactUsd(marketMoney.wastedShares)} sh)
                          </span>
                        )}
                      </span>
                      <span className="up">
                        Earned ${formatCompactUsd(marketMoney.earnedMoney)}
                      </span>
                      <span className={marketMoney.profitMoney >= 0 ? 'up' : 'down'}>
                        Profit {fmtSignedUsd(marketMoney.profitMoney)}
                      </span>
                    </span>
                  ) : (
                    (selectedMarketMeta?.pnl != null || selectedMarketActivity?.pnl != null) && (
                      <span
                        className={`wallet-daily-pnl ${
                          ((selectedMarketMeta?.pnl ?? selectedMarketActivity?.pnl) ?? 0) >= 0
                            ? 'up'
                            : 'down'
                        }`}
                      >
                        {fmtSignedUsd(selectedMarketMeta?.pnl ?? selectedMarketActivity?.pnl)}
                      </span>
                    )
                  )}
                </div>
              )}
              {showSideFlowBar && (
                <div className="wallet-bought-bar">
                  {(sideFlow.buyUp.shares > 0 || sideFlow.buyDown.shares > 0) && (
                    <span>
                      Bought
                      {sideFlow.buyUp.shares > 0 && (
                        <>
                          {' '}
                          <span className="up">Up</span>{' '}
                          <strong>
                            {formatCompactUsd(sideFlow.buyUp.shares)}
                            <span className="muted">
                              {' '}
                              (${formatCompactUsd(sideFlow.buyUp.usd)})
                            </span>
                          </strong>
                        </>
                      )}
                      {sideFlow.buyDown.shares > 0 && (
                        <>
                          {' '}
                          <span className="down">Down</span>{' '}
                          <strong>
                            {formatCompactUsd(sideFlow.buyDown.shares)}
                            <span className="muted">
                              {' '}
                              (${formatCompactUsd(sideFlow.buyDown.usd)})
                            </span>
                          </strong>
                        </>
                      )}
                    </span>
                  )}
                  {(sideFlow.sellUp.shares > 0 || sideFlow.sellDown.shares > 0) && (
                    <>
                      {(sideFlow.buyUp.shares > 0 || sideFlow.buyDown.shares > 0) && (
                        <span className="wallet-bought-sep">·</span>
                      )}
                      <span>
                        Sold
                        {sideFlow.sellUp.shares > 0 && (
                          <>
                            {' '}
                            <span className="up">Up</span>{' '}
                            <strong>
                              {formatCompactUsd(sideFlow.sellUp.shares)}
                              <span className="muted">
                                {' '}
                                (${formatCompactUsd(sideFlow.sellUp.usd)})
                              </span>
                            </strong>
                          </>
                        )}
                        {sideFlow.sellDown.shares > 0 && (
                          <>
                            {' '}
                            <span className="down">Down</span>{' '}
                            <strong>
                              {formatCompactUsd(sideFlow.sellDown.shares)}
                              <span className="muted">
                                {' '}
                                (${formatCompactUsd(sideFlow.sellDown.usd)})
                              </span>
                            </strong>
                          </>
                        )}
                      </span>
                    </>
                  )}
                </div>
              )}
              <div className="wallet-fill-list">
                {!activityLoading && !expandedMarket && (
                  <p className="muted wallet-fill-empty">Select a market</p>
                )}
                {!activityLoading && expandedMarket && marketFillLoading && !selectedMarketActivity && (
                  <p className="muted wallet-fill-empty">Loading fills…</p>
                )}
                {!activityLoading &&
                  expandedMarket &&
                  !marketFillLoading &&
                  !selectedMarketActivity && (
                  <p className="muted wallet-fill-empty">No fills</p>
                )}
                {selectedMarketActivity?.activity.map((row, i) => {
                  const typ = (row.type || '').toUpperCase()
                  const side = (row.side || '').toUpperCase()
                  const isRedeem = typ === 'REDEEM'
                  const isBuy = !isRedeem && side === 'BUY'
                  const isSell = !isRedeem && side === 'SELL'
                  const outcome = row.outcome || ''
                  const outcomeUp = outcome.toLowerCase() === 'up'
                  const afterWindow = isAfterMarketWindow(
                    Number(row.timestamp),
                    selectedMarketWindow,
                  )
                  const active =
                    activityHighlightTs != null &&
                    Math.abs(Number(row.timestamp) - activityHighlightTs) <= 750
                  const actionLabel = isRedeem
                    ? 'Redeem'
                    : side === 'BUY'
                      ? 'Buy'
                      : side === 'SELL'
                        ? 'Sell'
                        : typ || '—'
                  return (
                    <div
                      key={`${row.transaction_hash || 'x'}-${row.timestamp}-${i}`}
                      className={`wallet-fill-row${active ? ' active' : ''}${
                        afterWindow ? ' after-window' : ''
                      }`}
                      onMouseEnter={() => setActivityHighlightTs(Number(row.timestamp))}
                      onMouseLeave={() => setActivityHighlightTs(null)}
                    >
                      <span
                        className="wallet-fill-time"
                        title={
                          afterWindow && selectedMarketWindow
                            ? `After market window (${fmtTimeHm(selectedMarketWindow.startMs)}–${fmtTimeHm(selectedMarketWindow.endMs)} ET)`
                            : undefined
                        }
                      >
                        <span className="wallet-fill-time-clock">{fmtTimeShort(row.timestamp)}</span>
                        {afterWindow && <span className="wallet-fill-after">after</span>}
                      </span>
                      <span className="wallet-fill-action">
                        <span
                          className={
                            isRedeem ? undefined : isBuy ? 'up' : isSell ? 'down' : undefined
                          }
                        >
                          {actionLabel}
                        </span>{' '}
                        {outcome && (
                          <span className={outcomeUp ? 'up' : 'down'}>{outcome}</span>
                        )}
                      </span>
                      <span className="wallet-fill-size">
                        {formatCompactUsd(row.shares)}
                        {row.price != null ? (
                          <>
                            {' '}
                            <span className="muted">@</span> {formatCents(row.price)}
                          </>
                        ) : null}
                      </span>
                      <span className="wallet-fill-usd">${formatCompactUsd(row.usd)}</span>
                      <span className="wallet-fill-links">
                        {row.polygonscan_url && (
                          <a
                            href={row.polygonscan_url}
                            target="_blank"
                            rel="noreferrer"
                            title="Polygonscan"
                            onMouseEnter={(e) => e.stopPropagation()}
                          >
                            ↗
                          </a>
                        )}
                        {row.orbscan_url && (
                          <a
                            href={row.orbscan_url}
                            target="_blank"
                            rel="noreferrer"
                            title="Orbscan"
                            className="wallet-fill-orb"
                            onMouseEnter={(e) => e.stopPropagation()}
                          >
                            ◈
                          </a>
                        )}
                      </span>
                    </div>
                  )
                })}
              </div>
              <div className="wallet-panel-foot">
                <button
                  type="button"
                  className="wallet-fetch-more"
                  disabled={activityMoreLoading || activityLoading || !date}
                  onClick={() => void fetchMoreActivity()}
                >
                  {activityMoreLoading ? 'Fetching…' : 'Fetch more'}
                </button>
              </div>
            </div>

            <section className="wallet-chart-panel">
              <div className="wallet-chart-head">
                <div>
                  <h2>Price · trade timing</h2>
                  <p className="muted wallet-chart-sub">
                    {expandedMarket || selectedSlug
                      ? shortMarketLabel(
                          selectedMarketMeta?.title || selectedMarketActivity?.title,
                          selectedSlug,
                        )
                      : 'Select a market to load price charts'}
                    {traderMarks.length > 0
                      ? ` · ${traderMarks.length} fill${traderMarks.length === 1 ? '' : 's'} marked on Up/Down`
                      : ''}
                  </p>
                </div>
                {marketDetail?.market_id && (
                  <span className="wallet-chart-mid muted" title={marketDetail.market_id}>
                    {marketDetail.market_id}
                  </span>
                )}
              </div>
              {!(expandedMarket || selectedSlug) && (
                <p className="muted" style={{ padding: '0.75rem 0.9rem' }}>
                  Choose a market in PnL by market to see BTC and Up/Down charts.
                </p>
              )}
              {(expandedMarket || selectedSlug) && chartLoading && (
                <p className="muted" style={{ padding: '0.75rem 0.9rem' }}>
                  Loading price series…
                </p>
              )}
              {(expandedMarket || selectedSlug) &&
                !chartLoading &&
                chartError &&
                !marketDetail && (
                  <p className="muted" style={{ padding: '0.75rem 0.9rem' }}>
                    {chartError}
                    {traderMarks.length > 0
                      ? ' — fill markers still listed in Activity.'
                      : ''}
                  </p>
                )}
              {(expandedMarket || selectedSlug) &&
                !chartLoading &&
                marketDetail &&
                priceChartData.length > 0 && (
                  <div className="wallet-chart-stack">
                    <PriceChart
                      key={`${chartMarketKey}-btc`}
                      data={priceChartData}
                      priceToBeat={marketDetail.btc_open_price}
                      mode="btc"
                      title="BTC price"
                      seriesVisible={btcSeriesVisible}
                      onSeriesVisibleChange={setBtcSeriesVisible}
                      xDomain={chartXDomain}
                      onXDomainChange={setSharedXDomain}
                      onXDomainReset={() => setSharedXDomain(xDefaultDomain)}
                      xFullDomain={xFullDomain}
                      xDefaultDomain={xDefaultDomain}
                      hoverTime={sharedHoverTime}
                      onHoverTimeChange={setSharedHoverTime}
                      highlightTime={activityHighlightTs}
                    />
                    <PriceChart
                      key={`${chartMarketKey}-outcomes`}
                      data={priceChartData}
                      mode="outcomes"
                      title="Up / Down price"
                      xDomain={chartXDomain}
                      onXDomainChange={setSharedXDomain}
                      onXDomainReset={() => setSharedXDomain(xDefaultDomain)}
                      xFullDomain={xFullDomain}
                      xDefaultDomain={xDefaultDomain}
                      hoverTime={sharedHoverTime}
                      onHoverTimeChange={setSharedHoverTime}
                      traderMarks={traderMarks}
                      highlightTime={activityHighlightTs}
                    />
                  </div>
                )}
            </section>
          </section>
        )}
      </div>

      {deleteConfirm && (
        <div
          className="health-dialog-backdrop"
          onClick={cancelDeleteSaved}
          role="presentation"
        >
          <div
            className="health-dialog wallet-delete-dialog"
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="wallet-delete-title"
            aria-describedby="wallet-delete-desc"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="health-dialog-head">
              <div id="wallet-delete-title" className="health-dialog-title">
                Remove saved wallet?
              </div>
            </div>
            <p id="wallet-delete-desc" className="health-dialog-summary">
              Remove{' '}
              <strong>
                {displayWalletName(deleteConfirm.name, deleteConfirm.wallet)}
              </strong>{' '}
              ({shorten(deleteConfirm.wallet)}) from your saved list? Cached data for this
              wallet will be deleted.
            </p>
            <div className="health-dialog-actions">
              <button
                type="button"
                className="health-dialog-btn ghost"
                onClick={cancelDeleteSaved}
              >
                Cancel
              </button>
              <button
                type="button"
                className="health-dialog-btn primary wallet-delete-confirm"
                onClick={() => void confirmDeleteSaved()}
              >
                Remove
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
