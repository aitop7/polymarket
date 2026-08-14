const API_BASE = import.meta.env.VITE_API_BASE ?? ''

export type DataHealth = 'great' | 'good' | 'ok' | 'low' | 'bad' | 'unchecked'

export type MarketSummary = {
  market_id: string
  split: string
  start_time: number
  end_time: number
  rows: number
  winner: number | null
  /** From meta.json for TWAP/live; true when market resolved/closed. */
  closed?: boolean | null
  /** Epoch ms when Gamma/UMA reported resolution (meta.resolved_at). */
  resolved_at?: number | null
  /** Local parquet integrity: great|good|ok|low|bad|unchecked (meta.data_health). */
  data_health?: DataHealth | null
  /** Gap details (file + ET times) — shown on badge hover. */
  data_health_comment?: string | null
  btc_open_price: number | null
  has_features: boolean
  has_training: boolean
  date_et?: string
  time_et?: string
}

export type SeriesVolumeFields = {
  bn_buy?: number | null
  bn_sell?: number | null
  up_buy_vol?: number | null
  up_sell_vol?: number | null
  down_buy_vol?: number | null
  down_sell_vol?: number | null
}

export type MarketDetail = MarketSummary & {
  series: ({
    t: number
    btc: number | null
    up: number | null
    down: number | null
    twap?: number | null
    chainlink?: number | null
  } & SeriesVolumeFields)[]
  first: { timestamp: number; btc_price: number | null; up_price: number; down_price: number }
  last: { timestamp: number; btc_price: number | null; up_price: number; down_price: number }
}

export type BacktestFill = {
  timestamp?: number
  market_id?: string
  side?: string
  action?: string
  shares?: number
  price?: number
  usd?: number
  reason?: string
  model_p_up?: number | null
}

export type BacktestMarketRow = {
  market_id: string
  winner: number
  pnl: number
  n_fills: number
  ending_cash: number
  payout?: number
  fills?: BacktestFill[]
  equity?: { t: number; equity: number; cash: number }[]
  signals?: BacktestFill[]
}

export type BacktestStats = {
  opportunities_found: number
  markets_with_opportunities: number
  pairs_filled: number
  avg_net_edge: number | null
  fill_rate: number | null
}

export type BacktestResult = {
  strategy: string
  split: string
  date?: string | null
  n_markets: number
  total_pnl: number
  avg_pnl: number
  win_rate: number
  total_fills: number
  starting_cash: number
  shared_bankroll?: boolean
  ending_cash?: number
  markets: BacktestMarketRow[]
  equity_curve: { i: number; market_id: string; pnl: number; cum_pnl: number }[]
  params: Record<string, unknown>
  stats?: BacktestStats | null
}

export type BacktestMarketResult = {
  market_id: string
  winner: number
  starting_cash: number
  ending_cash: number
  pnl: number
  payout?: number
  n_fills: number
  fills: BacktestFill[]
  signals?: BacktestFill[]
  equity: { t: number; equity: number; cash: number }[]
}

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) },
    ...init,
  })
  if (!res.ok) {
    const text = await res.text()
    try {
      const body = JSON.parse(text) as { detail?: unknown; error?: unknown }
      const detail = body.detail ?? body.error
      if (typeof detail === 'string' && detail.trim()) {
        throw new Error(detail)
      }
      if (Array.isArray(detail) && detail.length) {
        const first = detail[0] as { msg?: string }
        if (first?.msg) throw new Error(first.msg)
      }
    } catch (err) {
      if (err instanceof Error && err.name === 'Error') throw err
    }
    throw new Error(text || res.statusText)
  }
  return res.json() as Promise<T>
}

export const api = {
  health: () => json<{ ok: boolean }>('/api/health'),
  strategies: () => json<{ name: string; description: string; params: Record<string, unknown> }[]>('/api/strategies'),
  strategiesCatalog: () =>
    json<{ strategies: StrategyCatalogItem[] }>('/api/strategies/catalog', { cache: 'no-store' }),
  strategyCatalogItem: (name: string) =>
    json<StrategyCatalogItem>(`/api/strategies/catalog/${encodeURIComponent(name)}`, { cache: 'no-store' }),
  strategyVersions: (name: string) =>
    json<StrategyVersionsResponse>(
      `/api/strategies/versions/${encodeURIComponent(name)}`,
      { cache: 'no-store' },
    ),
  strategyActiveVersion: (name: string) =>
    json<StrategyActiveResponse>(
      `/api/strategies/versions/${encodeURIComponent(name)}/active`,
      { cache: 'no-store' },
    ),
  strategyVersion: (name: string, versionId: string) =>
    json<StrategyVersionDetail>(
      `/api/strategies/versions/${encodeURIComponent(name)}/${encodeURIComponent(versionId)}`,
      { cache: 'no-store' },
    ),
  saveStrategyVersion: (
    name: string,
    body: {
      runtime_params: Record<string, unknown>
      train_params?: Record<string, unknown>
      label?: string
      kind?: string
      make_active?: boolean
    },
  ) =>
    json<StrategyVersionDetail>(`/api/strategies/versions/${encodeURIComponent(name)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  activateStrategyVersion: (name: string, versionId: string) =>
    json<StrategyVersionDetail>(
      `/api/strategies/versions/${encodeURIComponent(name)}/${encodeURIComponent(versionId)}/activate`,
      { method: 'POST' },
    ),
  lgbmModel: () => json<LgbmModelInfo>('/api/strategies/lgbm/model', { cache: 'no-store' }),
  lgbmTrainStatus: () => json<LgbmTrainJob>('/api/strategies/lgbm/train', { cache: 'no-store' }),
  lgbmTrain: (body?: {
    num_boost_round?: number
    early_stopping_rounds?: number
    max_markets?: number | null
  }) =>
    json<{ ok: boolean; job: LgbmTrainJob }>('/api/strategies/lgbm/train', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    }),
  momentumPairTrainStatus: () =>
    json<MomentumPairTrainJob>('/api/strategies/momentum_pair/train', { cache: 'no-store' }),
  momentumPairTrain: (body?: {
    horizon_seconds?: number
    delta_seconds?: number
    ema_period?: number
    train_ratio?: number
    num_boost_round?: number
    early_stopping_rounds?: number
    max_markets?: number | null
  }) =>
    json<{ ok: boolean; job: MomentumPairTrainJob }>('/api/strategies/momentum_pair/train', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    }),
  markets: (split: string, opts?: { limit?: number; date?: string; rebuild_index?: boolean }) => {
    const q = new URLSearchParams({ split })
    if (opts?.limit != null) q.set('limit', String(opts.limit))
    if (opts?.date) q.set('date', opts.date)
    if (opts?.rebuild_index) q.set('rebuild_index', 'true')
    return json<{ split: string; count: number; markets: MarketSummary[]; date?: string }>(`/api/markets?${q}`)
  },
  marketDates: (split: string, opts?: { rebuild_index?: boolean }) => {
    const q = new URLSearchParams({ split })
    if (opts?.rebuild_index) q.set('rebuild_index', 'true')
    return json<{ split: string; count: number; dates: string[]; min: string | null; max: string | null }>(
      `/api/markets/dates?${q}`,
    )
  },
  marketAt: (split: string, opts: { date?: string; time?: string; t?: number }) => {
    const q = new URLSearchParams({ split })
    if (opts.date) q.set('date', opts.date)
    if (opts.time) q.set('time', opts.time)
    if (opts.t != null) q.set('t', String(opts.t))
    return json<MarketSummary>(`/api/markets/at?${q}`)
  },
  market: (id: string, split?: string) =>
    json<MarketDetail>(`/api/markets/${id}${split ? `?split=${split}` : ''}`),
  neighbors: (id: string, split?: string) =>
    json<{ prev: string | null; next: string | null; split: string; index: number; total: number }>(
      `/api/markets/${id}/neighbors${split ? `?split=${split}` : ''}`,
    ),
  recheckMarketHealth: (id: string) =>
    json<{
      ok: boolean
      market_id: string
      pulled: boolean
      vps_enabled: boolean
      trade_rows_added?: number
      data_health: DataHealth
      data_health_comment?: string | null
      max_gap_ms?: number
      max_trade_quiet_ms?: number
      notes?: string[]
      notes_by_file?: Record<string, string[]>
      orderbooks_source?: string | null
    }>(`/api/markets/${id}/health/recheck`, { method: 'POST' }),
  repairMarket: (id: string) =>
    json<{
      ok: boolean
      market_id: string
      pulled: boolean
      vps_enabled: boolean
      trade_rows_added?: number
      data_health: DataHealth
      data_health_comment?: string | null
      max_gap_ms?: number
      max_trade_quiet_ms?: number
      notes?: string[]
      notes_by_file?: Record<string, string[]>
      orderbooks_source?: string | null
      vps_repair?: {
        ok?: boolean
        rows_added?: number
        rows_before?: number
        rows_after?: number
        rows_from_api?: number
        trades_mode?: string
        error?: string
        endpoint_missing?: boolean
      }
      filled?: Record<string, number>
      warning?: string | null
      error?: string | null
    }>(`/api/markets/${id}/repair`, { method: 'POST' }),
  generatePmOrderbooks: (id: string, opts?: { force?: boolean }) => {
    const q = opts?.force ? '?force=true' : ''
    return json<{
      ok: boolean
      market_id: string
      slug?: string
      path?: string
      n_rows?: number
      slot_ms?: number
      source?: string
      warning?: string | null
    }>(`/api/markets/${id}/pm-orderbooks${q}`, { method: 'POST' })
  },
  missingPmOrderbooks: (date?: string) => {
    const q = date ? `?date=${encodeURIComponent(date)}` : ''
    return json<{
      date: string | null
      n_total: number
      n_present: number
      n_missing: number
      missing: Array<{
        market_id: string
        slug?: string | null
        start_time: number
        end_time: number
        date_et?: string | null
        time_et?: string | null
        dir?: string | null
      }>
    }>(`/api/markets/pm-orderbooks/missing${q}`, { cache: 'no-store' })
  },
  generatePmChainlink: (id: string, opts?: { force?: boolean }) => {
    const q = opts?.force ? '?force=true' : ''
    return json<{
      ok: boolean
      market_id: string
      slug?: string
      path?: string
      n_rows?: number
      slot_ms?: number
      source?: string
      dates?: string[]
      warning?: string | null
    }>(`/api/markets/${id}/pm-chainlink${q}`, { method: 'POST' })
  },
  missingPmChainlink: (date?: string) => {
    const q = date ? `?date=${encodeURIComponent(date)}` : ''
    return json<{
      date: string | null
      n_total: number
      n_present: number
      n_missing: number
      missing: Array<{
        market_id: string
        slug?: string | null
        start_time: number
        end_time: number
        date_et?: string | null
        time_et?: string | null
        dir?: string | null
      }>
    }>(`/api/markets/pm-chainlink/missing${q}`, { cache: 'no-store' })
  },
  missingPmHealthRescore: (date?: string) => {
    const q = date ? `?date=${encodeURIComponent(date)}` : ''
    return json<{
      date: string | null
      n_total: number
      n_present: number
      n_missing: number
      missing: Array<{
        market_id: string
        slug?: string | null
        start_time: number
        end_time: number
        date_et?: string | null
        time_et?: string | null
        reasons?: string[]
        data_health?: string
      }>
    }>(`/api/markets/pmdata/health-rescore/missing${q}`, { cache: 'no-store' })
  },
  binanceHealth: (date?: string) => {
    const q = date ? `?date=${encodeURIComponent(date)}` : ''
    return json<{
      date: string | null
      n_total: number
      n_great: number
      n_issues: number
      counts: Record<string, number>
      markets: Array<{
        market_id: string
        slug?: string | null
        start_time: number
        end_time: number
        date_et?: string | null
        time_et?: string | null
        grade: string
        price_grade?: string
        trade_grade?: string
        max_gap_ms?: number
        max_trade_quiet_ms?: number
        has_price?: boolean
        has_trades?: boolean
      }>
    }>(`/api/markets/binance-health${q}`, { cache: 'no-store' })
  },
  repairBinance: (id: string) =>
    json<{
      ok: boolean
      market_id: string
      filled?: Record<string, number>
      grade?: string
      price_grade?: string
      trade_grade?: string
      max_gap_ms?: number
      max_trade_quiet_ms?: number
      has_price?: boolean
      has_trades?: boolean
      error?: string
    }>(`/api/markets/${encodeURIComponent(id)}/binance-repair`, { method: 'POST' }),
  rescorePmdataHealth: (id: string) =>
    json<{
      ok: boolean
      market_id: string
      rescored?: boolean
      was_needed?: boolean
      data_health: DataHealth
      data_health_comment?: string | null
      orderbooks_source?: string | null
      chainlink_source?: string | null
      max_gap_ms?: number
      max_trade_quiet_ms?: number
      notes?: string[]
      notes_by_file?: Record<string, string[]>
    }>(`/api/markets/${id}/health/rescore-pmdata`, { method: 'POST' }),
  book: (id: string, t?: number) =>
    json<Record<string, unknown>>(`/api/markets/${id}/book${t != null ? `?t=${t}` : ''}`),
  backtest: (body: Record<string, unknown>) =>
    json<BacktestResult>('/api/backtest', { method: 'POST', body: JSON.stringify(body) }),
  backtestMarket: (body: Record<string, unknown>) =>
    json<BacktestMarketResult>('/api/backtest/market', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  paperSession: (body: Record<string, unknown>) =>
    json<{ session_id: string; market_id: string; rows: number; speed: number }>(
      '/api/paper/session',
      { method: 'POST', body: JSON.stringify(body) },
    ),
  paperOrder: (body: Record<string, unknown>) =>
    json<{ ok: boolean }>('/api/paper/order', { method: 'POST', body: JSON.stringify(body) }),
  paperStatus: (sessionId: string) => json<Record<string, unknown>>(`/api/paper/${sessionId}`),
  liveState: () => json<LiveTick>('/api/live/state'),
  liveSeries: (marketId?: string | null, lookbackMs = 300_000) => {
    const q = new URLSearchParams()
    if (marketId) q.set('market_id', marketId)
    q.set('lookback_ms', String(lookbackMs))
    const qs = q.toString()
    return json<LiveSeriesResponse>(`/api/live/series${qs ? `?${qs}` : ''}`)
  },
  liveHolders: (limit = 20) => {
    const q = new URLSearchParams({
      limit: String(Math.max(1, Math.min(20, limit))),
      _ts: String(Date.now()),
    })
    return json<LiveHoldersResponse>(`/api/live/holders?${q}`, {
      cache: 'no-store',
    })
  },
  marketHolders: (marketId: string, limit = 20) => {
    const q = new URLSearchParams({
      limit: String(Math.max(1, Math.min(20, limit))),
      _ts: String(Date.now()),
    })
    return json<LiveHoldersResponse>(`/api/markets/${encodeURIComponent(marketId)}/holders?${q}`, {
      cache: 'no-store',
    })
  },
  marketActivity: (marketId: string, limit = 1500) => {
    const q = new URLSearchParams({
      limit: String(Math.max(1, Math.min(2000, limit))),
      _ts: String(Date.now()),
    })
    return json<{ market_id?: string; condition_id?: string | null; trades: LiveActivityTrade[] }>(
      `/api/markets/${encodeURIComponent(marketId)}/activity?${q}`,
      { cache: 'no-store' },
    )
  },
  marketTraders: (marketId: string, limit = 20) => {
    const q = new URLSearchParams({
      limit: String(Math.max(1, Math.min(50, limit))),
      _ts: String(Date.now()),
    })
    return json<MarketTradersResponse>(
      `/api/markets/${encodeURIComponent(marketId)}/traders?${q}`,
      { cache: 'no-store' },
    )
  },
  marketTraderDetail: (marketId: string, wallet: string) => {
    const q = new URLSearchParams({ _ts: String(Date.now()) })
    return json<TraderDetailResponse>(
      `/api/markets/${encodeURIComponent(marketId)}/traders/${encodeURIComponent(wallet)}?${q}`,
      { cache: 'no-store' },
    )
  },
  walletSummary: (address: string, opts?: { refresh?: boolean }) => {
    const q = new URLSearchParams({ _ts: String(Date.now()) })
    if (opts?.refresh) q.set('refresh', 'true')
    return json<WalletSummary & { cached?: boolean }>(
      `/api/wallets/${encodeURIComponent(address)}?${q}`,
      { cache: 'no-store' },
    )
  },
  walletPnl: (address: string, interval: WalletPnlInterval = '1d', opts?: { refresh?: boolean }) => {
    const q = new URLSearchParams({ interval, _ts: String(Date.now()) })
    if (opts?.refresh) q.set('refresh', 'true')
    return json<WalletPnlResponse & { cached?: boolean }>(
      `/api/wallets/${encodeURIComponent(address)}/pnl?${q}`,
      { cache: 'no-store' },
    )
  },
  walletTotalPnl: (address: string, interval: WalletTotalPnlInterval = '1w') => {
    const q = new URLSearchParams({ interval, _ts: String(Date.now()) })
    return json<WalletTotalPnlResponse>(
      `/api/wallets/${encodeURIComponent(address)}/total-pnl?${q}`,
      { cache: 'no-store' },
    )
  },
  walletDaily: (
    address: string,
    days = 90,
    opts?: { refresh?: boolean; scanLimit?: number; before?: string },
  ) => {
    const q = new URLSearchParams({ days: String(days), _ts: String(Date.now()) })
    if (opts?.refresh) q.set('refresh', 'true')
    if (opts?.scanLimit != null) q.set('scan_limit', String(opts.scanLimit))
    if (opts?.before) q.set('before', opts.before)
    return json<WalletDailyResponse & { cached?: boolean; has_more?: boolean; scan_limit?: number }>(
      `/api/wallets/${encodeURIComponent(address)}/daily?${q}`,
      { cache: 'no-store' },
    )
  },
  walletActivity: (
    address: string,
    opts?: { date?: string; slug?: string; limit?: number; offset?: number; refresh?: boolean },
  ) => {
    const q = new URLSearchParams({ _ts: String(Date.now()) })
    if (opts?.date) q.set('date', opts.date)
    if (opts?.slug) q.set('slug', opts.slug)
    if (opts?.limit != null) q.set('limit', String(opts.limit))
    if (opts?.offset != null) q.set('offset', String(opts.offset))
    if (opts?.refresh) q.set('refresh', 'true')
    return json<WalletActivityResponse & { cached?: boolean; has_more?: boolean; next_offset?: number }>(
      `/api/wallets/${encodeURIComponent(address)}/activity?${q}`,
      { cache: 'no-store' },
    )
  },
  walletMarkets: (
    address: string,
    opts?: { date?: string; limit?: number; activityLimit?: number; refresh?: boolean },
  ) => {
    const q = new URLSearchParams({ _ts: String(Date.now()) })
    if (opts?.date) q.set('date', opts.date)
    if (opts?.limit != null) q.set('limit', String(opts.limit))
    if (opts?.activityLimit != null) q.set('activity_limit', String(opts.activityLimit))
    if (opts?.refresh) q.set('refresh', 'true')
    return json<
      WalletMarketsResponse & { cached?: boolean; has_more?: boolean; total_count?: number }
    >(`/api/wallets/${encodeURIComponent(address)}/markets?${q}`, { cache: 'no-store' })
  },
  savedWallets: () =>
    json<{ count: number; wallets: SavedWalletRow[] }>('/api/wallets/saved', { cache: 'no-store' }),
  deleteSavedWallet: (address: string) =>
    json<{ ok: boolean; wallet: string }>(`/api/wallets/saved/${encodeURIComponent(address)}`, {
      method: 'DELETE',
      cache: 'no-store',
    }),
  saveWalletComment: (address: string, comment: string) =>
    json<{ ok: boolean; wallet: string; comment: string }>(
      `/api/wallets/saved/${encodeURIComponent(address)}/comment`,
      {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ comment }),
        cache: 'no-store',
      },
    ),
}

export type LiveSeriesPoint = {
  t: number
  up?: number | null
  down?: number | null
  btc?: number | null
  twap?: number | null
  chainlink?: number | null
} & SeriesVolumeFields

export type LiveSeriesResponse = {
  market_id?: string | null
  start_time?: number | null
  end_time?: number | null
  series: LiveSeriesPoint[]
  source?: { parquet?: number; twap_feed?: number; buffer?: number }
}

export type HolderRow = {
  proxy_wallet: string
  display_name: string
  amount: number
  profile_image?: string
  verified?: boolean
  outcome_index?: number | null
}

export type LiveHoldersResponse = {
  market_id?: string | null
  condition_id?: string | null
  updated_at?: number
  live?: boolean
  up: HolderRow[]
  down: HolderRow[]
}

/** Polymarket RTDS activity/trades row (live tape). */
export type LiveActivityTrade = {
  id: string
  timestamp: number
  name: string
  pseudonym?: string | null
  proxy_wallet: string
  profile_image?: string | null
  outcome: 'Up' | 'Down'
  side: 'BUY' | 'SELL'
  price: number
  shares: number
  usd: number
  transaction_hash?: string | null
  token?: boolean
  is_sell?: boolean
}

export type TraderStatRow = {
  wallet: string
  pnl: number
  volume_usd: number
  fills: number
  buy_usd?: number
  sell_usd?: number
  buy_fills?: number
  sell_fills?: number
  up_buy_shares?: number
  up_sell_shares?: number
  down_buy_shares?: number
  down_sell_shares?: number
  up_pos?: number
  down_pos?: number
  name?: string | null
}

export type TraderFillRow = {
  timestamp: number
  is_up: boolean
  is_buy: boolean
  price: number
  shares: number
  usd: number
  transaction_hash?: string | null
}

export type TraderDetailResponse = TraderStatRow & {
  market_id: string
  resolved: boolean
  winner: 'Up' | 'Down' | null
  fills_list: TraderFillRow[]
}

export type MarketTradersResponse = {
  market_id: string
  resolved: boolean
  winner: 'Up' | 'Down' | null
  by_pnl: TraderStatRow[]
  by_volume: TraderStatRow[]
}

export type WalletPnlInterval = '1d' | '1w' | '1m' | '1y' | 'ytd' | 'all'

export type WalletTotalPnlInterval = '1d' | '1w' | '1m' | 'all'

export type WalletSummary = {
  wallet: string
  name: string
  pseudonym?: string | null
  profile_image?: string | null
  positions_value: number
  biggest_win?: {
    realized_pnl: number
    title?: string | null
    slug?: string | null
    outcome?: string | null
  } | null
  total_pnl?: number | null
  /** Account-level maker + taker fee rebates (not BTC-only). */
  total_rebates?: number | null
  maker_rebates?: number | null
  taker_rebates?: number | null
  rebate_events?: number | null
  open_positions: number
  closed_sample: number
  polygonscan_url: string
  orbscan_url: string
  polymarket_url: string
  comment?: string | null
  scope?: string | null
}

export type WalletPnlPoint = { t: number; pnl: number }

export type WalletPnlResponse = {
  wallet: string
  interval: string
  fidelity: string
  start_pnl: number | null
  end_pnl: number | null
  pnl: number | null
  series: WalletPnlPoint[]
}

export type WalletTotalPnlPoint = {
  t: number
  pnl: number
  pnl_abs?: number
  fee: number
  reward: number
  deposit: number
  withdraw: number
}

export type WalletTotalPnlResponse = {
  wallet: string
  interval: string
  fidelity: string
  scope?: string
  pnl: number | null
  start_pnl?: number | null
  end_pnl?: number | null
  series: WalletTotalPnlPoint[]
}

export type WalletDailyRow = {
  date: string
  t: number
  /** Closed + activity-tape extras (aligned with PnL by market). */
  pnl: number
  /** Closed-settles-only PnL. */
  realized_pnl?: number | null
  /** Account-level fee rebates credited on this ET day. */
  rebates?: number | null
  cum_pnl?: number | null
  n_positions?: number
  n_open?: number
}

export type WalletDailyResponse = {
  wallet: string
  days: number
  traded_days?: number
  scan_limit?: number
  has_more?: boolean
  before?: string | null
  includes_open?: boolean
  by_market_day?: boolean
  markets_aligned?: boolean
  n_open?: number
  total_rebates?: number | null
  maker_rebates?: number | null
  taker_rebates?: number | null
  daily: WalletDailyRow[]
  cached?: boolean
}

export type WalletActivityItem = {
  timestamp: number
  type: string
  side?: string | null
  outcome?: string | null
  price?: number | null
  shares: number
  usd: number
  title?: string | null
  slug?: string | null
  condition_id?: string | null
  transaction_hash?: string | null
  proxy_wallet?: string
  name?: string | null
  pseudonym?: string | null
  icon?: string | null
  polygonscan_url?: string | null
  orbscan_url?: string | null
}

export type WalletMarketActivity = {
  condition_id?: string | null
  slug?: string | null
  title?: string | null
  icon?: string | null
  n_events: number
  volume_usd: number
  pnl?: number | null
  activity: WalletActivityItem[]
}

export type WalletActivityResponse = {
  wallet: string
  date?: string | null
  count: number
  offset?: number
  next_offset?: number
  has_more?: boolean
  name?: string | null
  activity: WalletActivityItem[]
  markets?: WalletMarketActivity[]
}

export type WalletMarketPnl = {
  condition_id?: string | null
  title?: string | null
  slug?: string | null
  icon?: string | null
  outcomes?: (string | null)[]
  pnl?: number | null
  pnl_source?: 'closed' | 'activity' | 'none' | string | null
  status?: string | null
  timestamp?: number | null
  end_date?: string | null
  total_bought?: number
  unredeemed?: boolean
  redeemable?: boolean
  open_shares?: number | null
  open_value?: number | null
}

export type WalletMarketsResponse = {
  wallet: string
  date?: string | null
  count: number
  total_count?: number
  has_more?: boolean
  total_pnl: number
  closed_pnl?: number
  activity_pnl?: number
  markets: WalletMarketPnl[]
}

export type SavedWalletRow = {
  wallet: string
  name?: string | null
  profile_image?: string | null
  positions_value?: number | null
  total_pnl?: number | null
  comment?: string | null
  updated_at?: number
  last_viewed_at?: number
}

export type StrategyDataRequirement = {
  name: string
  path: string
  why: string
}

export type StrategyCatalogItem = {
  name: string
  title?: string
  description?: string
  idea?: string
  when_to_use?: string
  data_required?: StrategyDataRequirement[]
  trainable?: boolean
  train_defaults?: Record<string, unknown>
  runtime_params?: Record<string, unknown>
  params?: Record<string, unknown>
  outputs?: string[]
}

export type StrategyVersionSummary = {
  id: string
  strategy: string
  created_at?: string | null
  label?: string
  kind?: string
  runtime_params?: Record<string, unknown>
  train_params?: Record<string, unknown>
  metrics_summary?: Record<string, unknown> | null
  has_model?: boolean
  path?: string
  active?: boolean
}

export type StrategyVersionsResponse = {
  strategy: string
  dir: string
  active_version_id?: string | null
  count: number
  versions: StrategyVersionSummary[]
}

export type StrategyVersionDetail = {
  id: string
  strategy: string
  created_at?: string
  label?: string
  kind?: string
  runtime_params?: Record<string, unknown>
  train_params?: Record<string, unknown>
  metrics_summary?: Record<string, unknown> | null
  artifacts?: Record<string, string>
  path?: string
  active?: boolean
}

export type StrategyActiveResponse = {
  strategy: string
  active: boolean
  version: StrategyVersionDetail & { id: string | null }
}

export type LgbmTrainJob = {
  status: 'idle' | 'running' | 'succeeded' | 'failed' | string
  started_at?: string | null
  finished_at?: string | null
  error?: string | null
  log_tail?: string[]
  metrics?: Record<string, unknown> | null
  params?: Record<string, unknown> | null
  pid?: number | null
  log_path?: string | null
  version?: { id?: string; path?: string } | null
}

export type MomentumPairTrainJob = {
  status: 'idle' | 'running' | 'succeeded' | 'failed' | string
  progress?: number
  phase?: string | null
  message?: string | null
  started_at?: string | null
  finished_at?: string | null
  error?: string | null
  params?: Record<string, unknown> | null
  metrics?: Record<string, unknown> | null
  version?: { id?: string; path?: string } | null
}

export type LgbmModelInfo = {
  models_dir: string
  features_dir: string
  model_path: string
  model_exists: boolean
  model_mtime?: string | null
  metrics_path?: string
  metrics?: Record<string, unknown> | null
  feature_names?: string[] | null
  schema_features?: string[]
  n_schema_features?: number
  splits?: Record<string, { path: string; exists: boolean; n_markets: number }>
  train_job?: LgbmTrainJob
}

export type LiveTick = {
  type: string
  live?: boolean
  timestamp: number
  market_id?: string | null
  slug?: string | null
  start_time?: number | null
  end_time?: number | null
  btc_price?: number | null
  price_to_beat?: number | null
  btc_open?: number | null
  btc_twap_30s?: number | null
  btc_chainlink?: number | null
  up_price?: number
  down_price?: number
  remaining_seconds?: number
  elapsed_seconds?: number
  book?: Record<string, unknown>
  error?: string
  message?: string
}

export function wsUrl(path: string): string {
  const base = API_BASE || `${window.location.protocol}//${window.location.host}`
  const u = new URL(path, base)
  u.protocol = u.protocol === 'https:' ? 'wss:' : 'ws:'
  return u.toString()
}

function isWholeAfterDigits(n: number, digits: number): boolean {
  const factor = 10 ** digits
  const rounded = Math.round(Math.abs(n) * factor) / factor
  return Math.abs(rounded - Math.round(rounded)) < 1e-12
}

/** Drop trailing zeros / decimal when the fractional part is zero (e.g. 12.00 → 12). */
function formatFixedTrim(n: number, digits: number): string {
  if (isWholeAfterDigits(n, digits)) return String(Math.round(n))
  return n.toFixed(digits)
}

export function formatUsd(n: number | null | undefined, digits = 2): string {
  if (n == null || Number.isNaN(n)) return '—'
  const minFrac = isWholeAfterDigits(n, digits) ? 0 : digits
  return n.toLocaleString(undefined, {
    minimumFractionDigits: minFrac,
    maximumFractionDigits: digits,
  })
}

/** Format probability/price as cents with up to 2 fractional digits (e.g. 51.48¢, 51¢). */
export function formatCents(p: number | null | undefined): string {
  if (p == null || Number.isNaN(p)) return '—¢'
  return `${formatFixedTrim(p * 100, 2)}¢`
}

/** Format probability/price as whole cents (e.g. 51¢). */
export function formatCentsInt(p: number | null | undefined): string {
  if (p == null || Number.isNaN(p)) return '—¢'
  return `${Math.round(p * 100)}¢`
}

/**
 * Trade / order-book display: whole cents mid-range, one decimal at the extremes
 * (≤1¢ or ≥90¢) where Polymarket uses finer ticks. Whole values stay integers (no .0).
 */
export function formatCentsTrade(p: number | null | undefined): string {
  if (p == null || Number.isNaN(p)) return '—¢'
  const c = p * 100
  if (c <= 1 || c >= 90) {
    return `${formatFixedTrim(c, 1)}¢`
  }
  return `${Math.round(c)}¢`
}

function formatCentsBound(cents: number): string {
  const c = Math.max(0, Math.min(100, cents))
  if (c <= 1 || c >= 90) {
    return formatFixedTrim(c, 1)
  }
  return String(Math.round(c))
}

/** Same adaptive rule as formatCentsTrade, without the ¢ suffix (for ranges). */
export function formatCentsTradeNumber(p: number): string {
  return formatCentsBound(p * 100)
}

export function formatPct(p: number | null | undefined): string {
  if (p == null || Number.isNaN(p)) return '—'
  return `${(p * 100).toFixed(2)}%`
}

export function formatWindow(startMs: number, endMs: number): string {
  const s = new Date(startMs)
  const e = new Date(endMs)
  const opts: Intl.DateTimeFormatOptions = {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  }
  return `${s.toLocaleString(undefined, opts)} – ${e.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })}`
}

/** e.g. "Aug 8, 12:44:13 PM ET" */
export function formatResolvedEt(ms: number): string {
  try {
    return (
      new Intl.DateTimeFormat('en-US', {
        timeZone: 'America/New_York',
        month: 'short',
        day: 'numeric',
        hour: 'numeric',
        minute: '2-digit',
        second: '2-digit',
        hour12: true,
      }).format(new Date(ms)) + ' ET'
    )
  } catch {
    return ''
  }
}

/** Polymarket-style window: "August 7, 12:50-12:55AM ET" */
export function formatWindowEt(startMs: number, endMs: number): string {
  const optsDate: Intl.DateTimeFormatOptions = {
    timeZone: 'America/New_York',
    month: 'long',
    day: 'numeric',
  }
  const optsTime: Intl.DateTimeFormatOptions = {
    timeZone: 'America/New_York',
    hour: 'numeric',
    minute: '2-digit',
    hour12: true,
  }
  const s = new Date(startMs)
  const e = new Date(endMs)
  const date = new Intl.DateTimeFormat('en-US', optsDate).format(s)
  const t0 = new Intl.DateTimeFormat('en-US', optsTime).format(s).replace(' ', '')
  const t1 = new Intl.DateTimeFormat('en-US', optsTime).format(e).replace(' ', '')
  // Collapse duplicate AM/PM when same meridian
  const ampm = (t: string) => (t.toUpperCase().includes('PM') ? 'PM' : 'AM')
  let range: string
  if (ampm(t0) === ampm(t1)) {
    const startNoMer = t0.replace(/\s?(AM|PM)/i, '')
    range = `${startNoMer}-${t1}`
  } else {
    range = `${t0}-${t1}`
  }
  return `${date}, ${range} ET`
}

/** Compact label for selects: "Jul 8, 2026, 8:00–8:05 PM" */
export function formatMarketLabel(startMs: number, endMs: number, marketId?: string): string {
  const s = new Date(startMs)
  const e = new Date(endMs)
  const date = s.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
  const t0 = s.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })
  const t1 = e.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })
  const window = `${date}, ${t0} – ${t1}`
  return marketId ? `${window} · ${marketId}` : window
}
