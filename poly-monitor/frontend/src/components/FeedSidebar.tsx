import type { LiveActivityTrade, LiveHoldersResponse, MarketTradersResponse, TraderDetailResponse } from '../api'
import ActivityPanel from './ActivityPanel'
import HoldersPanel from './HoldersPanel'
import TradersPanel from './TradersPanel'

type Props = {
  enabled?: boolean
  live?: boolean
  /** History mode: show earners table only (hide holders + activity) */
  showTraders?: boolean
  marketId?: string | null
  traders?: MarketTradersResponse | null
  selectedWallet?: string | null
  onSelectWallet?: (wallet: string | null) => void
  onTraderDetailChange?: (detail: TraderDetailResponse | null) => void
  playheadTs?: number | null
  holders?: LiveHoldersResponse | null
  holdersRevision?: number
  activityTrades?: LiveActivityTrade[]
  nowMs?: number
}

export default function FeedSidebar({
  enabled = true,
  live = false,
  showTraders = false,
  marketId = null,
  traders = null,
  selectedWallet = null,
  onSelectWallet,
  onTraderDetailChange,
  playheadTs = null,
  holders = null,
  holdersRevision = 0,
  activityTrades = [],
  nowMs,
}: Props) {
  return (
    <aside className="workspace-rail workspace-rail-right">
      {showTraders ? (
        <TradersPanel
          enabled={enabled}
          marketId={marketId}
          data={traders}
          selectedWallet={selectedWallet}
          onSelectWallet={onSelectWallet}
          activityTrades={activityTrades}
          onDetailChange={onTraderDetailChange}
          playheadTs={playheadTs}
        />
      ) : (
        <>
          <HoldersPanel enabled={enabled} live={live} data={holders} revision={holdersRevision} />
          <ActivityPanel
            enabled={enabled}
            live={live}
            trades={activityTrades}
            nowMs={nowMs}
            selectedWallet={selectedWallet}
            onSelectWallet={onSelectWallet}
          />
        </>
      )}
    </aside>
  )
}
