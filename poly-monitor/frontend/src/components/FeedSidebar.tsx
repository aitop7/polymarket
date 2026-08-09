import type { LiveActivityTrade, LiveHoldersResponse } from '../api'
import ActivityPanel from './ActivityPanel'
import HoldersPanel from './HoldersPanel'

type Props = {
  enabled?: boolean
  live?: boolean
  holders?: LiveHoldersResponse | null
  holdersRevision?: number
  activityTrades?: LiveActivityTrade[]
  nowMs?: number
}

export default function FeedSidebar({
  enabled = true,
  live = false,
  holders = null,
  holdersRevision = 0,
  activityTrades = [],
  nowMs,
}: Props) {
  return (
    <aside className="workspace-rail workspace-rail-right">
      <HoldersPanel enabled={enabled} live={live} data={holders} revision={holdersRevision} />
      <ActivityPanel enabled={enabled} live={live} trades={activityTrades} nowMs={nowMs} />
    </aside>
  )
}
