import type { LiveActivityTrade, LiveHoldersResponse } from '../api'
import ActivityPanel from './ActivityPanel'
import HoldersPanel from './HoldersPanel'

type Props = {
  liveHolders?: boolean
  holders?: LiveHoldersResponse | null
  holdersRevision?: number
  liveActivity?: boolean
  activityTrades?: LiveActivityTrade[]
  nowMs?: number
}

export default function FeedSidebar({
  liveHolders = false,
  holders = null,
  holdersRevision = 0,
  liveActivity = false,
  activityTrades = [],
  nowMs,
}: Props) {
  return (
    <aside className="workspace-rail workspace-rail-right">
      <HoldersPanel enabled={liveHolders} data={holders} revision={holdersRevision} />
      <ActivityPanel enabled={liveActivity} trades={activityTrades} nowMs={nowMs} />
    </aside>
  )
}
