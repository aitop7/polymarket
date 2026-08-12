import { useEffect, useMemo, useRef, useState, type MouseEvent } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import {
  Area,
  CartesianGrid,
  ComposedChart,
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
      redeemUsd += usd > 0 ? usd : shares // redeem pays ~$1/share
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
  const loadGen = useRef(0)

  const [summary, setSummary] = useState<WalletSummary | null>(null)
  const [pnl, setPnl] = useState<WalletPnlResponse | null>(null)
  const [daily, setDaily] = useState<WalletDailyRow[]>([])
  const [dailyHasMore, setDailyHasMore] = useState(false)
  const [dailyScanLimit, setDailyScanLimit] = useState(3000)
  const [activityMarkets, setActivityMarkets] = useState<WalletMarketActivity[]>([])
  const [activityNextOffset, setActivityNextOffset] = useState(0)
  const [activityHasMore, setActivityHasMore] = useState(false)
  const [activityLimit, setActivityLimit] = useState(500)
  const [marketPnls, setMarketPnls] = useState<WalletMarketPnl[]>([])
  const [marketsTotalPnl, setMarketsTotalPnl] = useState<number | null>(null)
  const [marketsHasMore, setMarketsHasMore] = useState(false)
  const [marketsLimit, setMarketsLimit] = useState(200)
  const [expandedMarket, setExpandedMarket] = useState<string | null>(null)
  const [savedWallets, setSavedWallets] = useState<SavedWalletRow[]>([])
  const [fromCache, setFromCache] = useState(false)

  const [loading, setLoading] = useState(false)
  const [pnlLoading, setPnlLoading] = useState(false)
  const [activityLoading, setActivityLoading] = useState(false)
  const [dailyMoreLoading, setDailyMoreLoading] = useState(false)
  const [marketsMoreLoading, setMarketsMoreLoading] = useState(false)
  const [activityMoreLoading, setActivityMoreLoading] = useState(false)
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
    setMarketPnls([])
    setMarketsTotalPnl(null)
    setExpandedMarket(null)
    setMarketDetail(null)
    setChartError(null)
    setActivityHighlightTs(null)
    setPnl(null)
    setDailyScanLimit(3000)
    setActivityLimit(500)
    setMarketsLimit(200)
    setCommentSavedAt(null)
    commentWalletRef.current = normalized

    // Instant profile paint from the saved-wallet list while cache/API loads.
    const savedHit = savedWallets.find((w) => w.wallet === normalized)
    if (savedHit && !refresh) {
      setCommentDraft(savedHit.comment || '')
      setSummary({
        wallet: normalized,
        name: savedHit.name || shorten(normalized),
        profile_image: savedHit.profile_image ?? null,
        positions_value: Number(savedHit.positions_value ?? 0),
        total_pnl: savedHit.total_pnl ?? null,
        biggest_win: null,
        open_positions: 0,
        closed_sample: 0,
        polygonscan_url: `https://polygonscan.com/address/${normalized}`,
        orbscan_url: `https://orbscan.com/address/${normalized}`,
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
    setActivityLoading(true)
    setError(null)
    try {
      const [sum, dailyRes, pnlRes, act, mkts] = await Promise.all([
        api.walletSummary(addr, { refresh: true }),
        api.walletDaily(addr, 120, { refresh: true, scanLimit: scan }),
        api.walletPnl(addr, interval, { refresh: true }),
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
      setActivityMarkets(act.markets || [])
      setActivityNextOffset(act.next_offset ?? act.count ?? 0)
      setActivityHasMore(Boolean(act.has_more))
      setMarketPnls(mkts.markets || [])
      setMarketsTotalPnl(mkts.total_pnl ?? null)
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
      setMarketPnls(mkts.markets || [])
      setMarketsTotalPnl(mkts.total_pnl ?? null)
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
      const beforeCount =
        selectedKey == null
          ? 0
          : (activityMarkets.find((m) => (m.condition_id || m.slug || m.title || 'unknown') === selectedKey)
              ?.activity.length ?? 0)

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

      const countForSelected = (list: typeof markets) => {
        if (!selectedKey) return 0
        const hit = list.find(
          (m) => (m.condition_id || m.slug || m.title || 'unknown') === selectedKey,
        )
        return hit?.activity.length ?? 0
      }

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
          byKey.set(m.condition_id || m.slug || m.title || 'unknown', m)
        }
        for (const m of incoming) {
          const key = m.condition_id || m.slug || m.title || 'unknown'
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

      setActivityLimit(deepLimit)
      setActivityMarkets(markets)
      setActivityNextOffset(nextOffset)
      setActivityHasMore(hasMore)
      setFromCache(false)

      // Keep header counts in sync for the open market.
      if (selectedKey) {
        const hit = markets.find(
          (m) => (m.condition_id || m.slug || m.title || 'unknown') === selectedKey,
        )
        if (hit && hit.n_events !== hit.activity.length) {
          hit.n_events = hit.activity.length
        }
      }
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
      setMarketPnls([])
      setMarketsTotalPnl(null)
      setMarketsHasMore(false)
      setActivityHasMore(false)
      setPnl(null)
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
    if (!wallet || !date) {
      setActivityMarkets([])
      setMarketPnls([])
      setMarketsTotalPnl(null)
      setMarketsHasMore(false)
      setActivityHasMore(false)
      setActivityNextOffset(0)
      return
    }
    let cancelled = false
    setActivityLoading(true)
    setExpandedMarket(null)
    setActivityHighlightTs(null)
    Promise.all([
      api.walletActivity(wallet, { date, limit: activityLimit }),
      api.walletMarkets(wallet, {
        date,
        limit: marketsLimit,
        activityLimit,
      }),
    ])
      .then(([act, mkts]) => {
        if (cancelled) return
        setActivityMarkets(act.markets || [])
        setActivityNextOffset(act.next_offset ?? act.count ?? 0)
        setActivityHasMore(Boolean(act.has_more))
        setMarketPnls(mkts.markets || [])
        setMarketsTotalPnl(mkts.total_pnl ?? null)
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
          setMarketPnls([])
          setMarketsTotalPnl(null)
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

  const selectedMarketActivity = useMemo(() => {
    if (!expandedMarket) return null
    return (
      activityMarkets.find((m) => {
        const key = m.condition_id || m.slug || m.title || 'unknown'
        return key === expandedMarket
      }) ?? null
    )
  }, [activityMarkets, expandedMarket])

  const selectedMarketMeta = useMemo(() => {
    if (!expandedMarket) return null
    return (
      marketPnls.find((m) => {
        const key = m.condition_id || m.slug || m.title || 'm'
        return key === expandedMarket
      }) ?? null
    )
  }, [marketPnls, expandedMarket])

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

  const selectedSlug =
    selectedMarketActivity?.slug || selectedMarketMeta?.slug || null

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
      return
    }
    let cancelled = false
    setChartLoading(true)
    setChartError(null)
    setSharedHoverTime(null)

    ;(async () => {
      try {
        const startMs = slugToWindowStartMs(selectedSlug)
        let marketId: string | null = null
        if (startMs != null) {
          try {
            const hit = await api.marketAt('twap', { t: startMs + 15_000 })
            marketId = hit.market_id
          } catch {
            /* fall through */
          }
        }
        if (!marketId) {
          if (!cancelled) {
            setMarketDetail(null)
            setChartError('No local price history for this market window')
          }
          return
        }
        const detail = await api.market(marketId, 'twap')
        if (cancelled) return
        setMarketDetail(detail)
        if (detail.start_time != null && detail.end_time != null) {
          setSharedXDomain([detail.start_time, detail.end_time])
        } else if (detail.series?.length) {
          setSharedXDomain([
            detail.series[0].t,
            detail.series[detail.series.length - 1].t,
          ])
        }
      } catch (e) {
        if (!cancelled) {
          setMarketDetail(null)
          setChartError(e instanceof Error ? e.message : String(e))
        }
      } finally {
        if (!cancelled) setChartLoading(false)
      }
    })()

    return () => {
      cancelled = true
    }
  }, [selectedSlug])

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
    if (marketDetail?.start_time != null && marketDetail?.end_time != null) {
      return [marketDetail.start_time, marketDetail.end_time]
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

  const chartXDomain = sharedXDomain ?? xFullDomain

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

        {wallet && (expandedMarket || selectedSlug) && (
          <section className="wallet-chart-panel">
            <div className="wallet-chart-head">
              <div>
                <h2>Price · trade timing</h2>
                <p className="muted wallet-chart-sub">
                  {shortMarketLabel(
                    selectedMarketMeta?.title || selectedMarketActivity?.title,
                    selectedSlug,
                  )}
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
            {chartLoading && (
              <p className="muted" style={{ padding: '0.75rem 0.9rem' }}>
                Loading price series…
              </p>
            )}
            {!chartLoading && chartError && !marketDetail && (
              <p className="muted" style={{ padding: '0.75rem 0.9rem' }}>
                {chartError}
                {traderMarks.length > 0
                  ? ' — fill markers still listed in Activity below.'
                  : ''}
              </p>
            )}
            {!chartLoading && marketDetail && priceChartData.length > 0 && (
              <div className="wallet-chart-stack">
                <PriceChart
                  data={priceChartData}
                  priceToBeat={marketDetail.btc_open_price}
                  mode="btc"
                  title="BTC price"
                  seriesVisible={btcSeriesVisible}
                  onSeriesVisibleChange={setBtcSeriesVisible}
                  xDomain={chartXDomain}
                  onXDomainChange={setSharedXDomain}
                  onXDomainReset={() => setSharedXDomain(xFullDomain)}
                  xFullDomain={xFullDomain}
                  xDefaultDomain={xFullDomain}
                  hoverTime={sharedHoverTime}
                  onHoverTimeChange={setSharedHoverTime}
                  highlightTime={activityHighlightTs}
                />
                <PriceChart
                  data={priceChartData}
                  mode="outcomes"
                  title="Up / Down price"
                  xDomain={chartXDomain}
                  onXDomainChange={setSharedXDomain}
                  onXDomainReset={() => setSharedXDomain(xFullDomain)}
                  xFullDomain={xFullDomain}
                  xDefaultDomain={xFullDomain}
                  hoverTime={sharedHoverTime}
                  onHoverTimeChange={setSharedHoverTime}
                  traderMarks={traderMarks}
                  highlightTime={activityHighlightTs}
                />
              </div>
            )}
          </section>
        )}

        {wallet && (
          <section className="wallet-split-row wallet-split-row-3">
            <div className="wallet-panel">
              <div className="wallet-panel-head">
                <h2>PnL by day</h2>
                <span className="muted">{daily.length} days</span>
              </div>
              <div className="wallet-daily-list">
                {daily.length === 0 && (
                  <p className="muted" style={{ padding: '0.75rem' }}>
                    {loading ? 'Loading…' : 'No daily PnL yet'}
                  </p>
                )}
                {daily.map((row) => (
                  <button
                    key={row.date}
                    type="button"
                    className={`wallet-daily-row${date === row.date ? ' active' : ''}`}
                    onClick={() => setDate(row.date)}
                  >
                    <span className="wallet-daily-date">{row.date}</span>
                    <span className={`wallet-daily-pnl ${row.pnl >= 0 ? 'up' : 'down'}`}>
                      {fmtSignedUsd(row.pnl)}
                    </span>
                  </button>
                ))}
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
                <span className="muted">
                  {activityLoading
                    ? 'Loading…'
                    : `${marketPnls.length} mkts${
                        marketsTotalPnl != null ? ` · ${fmtSignedUsd(marketsTotalPnl)}` : ''
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
                  const key = m.condition_id || m.slug || m.title || 'm'
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
                    <span className="muted">{expandedMarket ? 'None' : '—'}</span>
                  )}
                </div>
              </div>
              {(selectedMarketMeta?.title ||
                selectedMarketMeta?.slug ||
                selectedMarketActivity?.title ||
                selectedMarketActivity?.slug) && (
                <div className="wallet-activity-selected-title">
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
                {!activityLoading && expandedMarket && !selectedMarketActivity && (
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
                      className={`wallet-fill-row${active ? ' active' : ''}`}
                      onMouseEnter={() => setActivityHighlightTs(Number(row.timestamp))}
                      onMouseLeave={() => setActivityHighlightTs(null)}
                    >
                      <span className="wallet-fill-time">{fmtTimeShort(row.timestamp)}</span>
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
