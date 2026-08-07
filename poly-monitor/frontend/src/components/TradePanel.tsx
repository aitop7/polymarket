import { formatPct, formatUsd } from '../api'

type Props = {
  side: 'UP' | 'DOWN'
  onSide: (s: 'UP' | 'DOWN') => void
  amount: number
  onAmount: (n: number) => void
  onTrade: () => void
  disabled?: boolean
  upPrice: number
  downPrice: number
  cash?: number
  modelPUp?: number | null
}

export default function TradePanel({
  side,
  onSide,
  amount,
  onAmount,
  onTrade,
  disabled,
  upPrice,
  downPrice,
  cash,
  modelPUp,
}: Props) {
  const price = side === 'UP' ? upPrice : downPrice
  const shares = price > 0 ? amount / price : 0

  return (
    <div className="panel trade-panel">
      <div className="side-toggle">
        <button type="button" className={`up ${side === 'UP' ? 'active' : ''}`} onClick={() => onSide('UP')}>
          Buy Up {formatPct(upPrice)}
        </button>
        <button
          type="button"
          className={`down ${side === 'DOWN' ? 'active' : ''}`}
          onClick={() => onSide('DOWN')}
        >
          Buy Down {formatPct(downPrice)}
        </button>
      </div>
      <label htmlFor="amount">Amount (USD)</label>
      <input
        id="amount"
        type="number"
        min={1}
        step={1}
        value={amount}
        onChange={(e) => onAmount(Number(e.target.value))}
      />
      <div className="muted" style={{ marginBottom: '0.75rem', fontSize: '0.85rem' }}>
        ~{shares.toFixed(2)} shares @ {formatUsd(price, 3)}
        {cash != null && (
          <>
            {' '}
            · Cash {formatUsd(cash)}
          </>
        )}
        {modelPUp != null && (
          <>
            {' '}
            · Model P(UP) {formatPct(modelPUp)}
          </>
        )}
      </div>
      <button
        type="button"
        className={`btn-primary ${side === 'UP' ? 'up' : 'down'}`}
        disabled={disabled}
        onClick={onTrade}
      >
        Trade
      </button>
    </div>
  )
}
