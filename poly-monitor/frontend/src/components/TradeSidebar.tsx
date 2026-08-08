import type { LiveHoldersResponse } from '../api'
import HoldersPanel from './HoldersPanel'
import OutcomeCard from './OutcomeCard'
import TradeCard from './TradeCard'

type Props = {
  mode: 'monitor' | 'paper'
  tradeAction: 'BUY' | 'SELL'
  onTradeAction: (a: 'BUY' | 'SELL') => void
  side: 'UP' | 'DOWN'
  onSide: (s: 'UP' | 'DOWN') => void
  onTrade: (opts: { size_usd?: number; shares?: number }) => void
  upPrice: number
  downPrice: number
  upHasAsk?: boolean
  downHasAsk?: boolean
  upHasBid?: boolean
  downHasBid?: boolean
  cash?: number
  heldShares?: number
  tradeDisabled: boolean
  monitorHint?: boolean
  liveHolders?: boolean
  holders?: LiveHoldersResponse | null
  holdersRevision?: number
  onReloadHolders?: () => void | Promise<void>
  holdersReloading?: boolean
  /** History stopped: show settled outcome instead of trade card */
  showOutcome?: boolean
  outcome?: 'Up' | 'Down' | 'not_closed' | null
  outcomeSubtitle?: string
}

export default function TradeSidebar(props: Props) {
  const {
    mode,
    tradeAction,
    onTradeAction,
    side,
    onSide,
    onTrade,
    upPrice,
    downPrice,
    upHasAsk,
    downHasAsk,
    upHasBid,
    downHasBid,
    cash,
    heldShares,
    tradeDisabled,
    monitorHint,
    liveHolders = false,
    holders = null,
    holdersRevision = 0,
    onReloadHolders,
    holdersReloading = false,
    showOutcome = false,
    outcome = null,
    outcomeSubtitle = '',
  } = props

  return (
    <aside className="control-sidebar control-sidebar-right">
      {showOutcome ? (
        <OutcomeCard outcome={outcome} subtitle={outcomeSubtitle} />
      ) : (
        <TradeCard
          monitorHint={monitorHint ?? mode === 'monitor'}
          tradeAction={tradeAction}
          onTradeAction={onTradeAction}
          side={side}
          onSide={onSide}
          upPrice={upPrice}
          downPrice={downPrice}
          upHasAsk={upHasAsk}
          downHasAsk={downHasAsk}
          upHasBid={upHasBid}
          downHasBid={downHasBid}
          cash={cash}
          heldShares={heldShares}
          onTrade={onTrade}
          tradeDisabled={tradeDisabled}
        />
      )}
      <HoldersPanel
        enabled={liveHolders}
        data={holders}
        revision={holdersRevision}
        onReload={onReloadHolders}
        reloading={holdersReloading}
      />
    </aside>
  )
}
