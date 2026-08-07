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
  cash?: number
  heldShares?: number
  tradeDisabled: boolean
  monitorHint?: boolean
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
    cash,
    heldShares,
    tradeDisabled,
    monitorHint,
  } = props

  return (
    <aside className="control-sidebar control-sidebar-right">
      <TradeCard
        monitorHint={monitorHint ?? mode === 'monitor'}
        tradeAction={tradeAction}
        onTradeAction={onTradeAction}
        side={side}
        onSide={onSide}
        upPrice={upPrice}
        downPrice={downPrice}
        cash={cash}
        heldShares={heldShares}
        onTrade={onTrade}
        tradeDisabled={tradeDisabled}
      />
    </aside>
  )
}
