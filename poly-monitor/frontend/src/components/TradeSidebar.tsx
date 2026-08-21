import TradeCard from './TradeCard'
import type { MarketSeriesKey } from '../series'
import { seriesTitle } from '../series'

type Props = {
  mode: 'monitor' | 'paper'
  marketSeries?: MarketSeriesKey
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
}

export default function TradeSidebar(props: Props) {
  const {
    mode,
    marketSeries = '5m',
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
  } = props

  return (
    <div className="trade-stack">
      <TradeCard
        title={seriesTitle(marketSeries)}
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
    </div>
  )
}
