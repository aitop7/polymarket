import { useEffect, useRef, useState } from 'react'
import { formatCentsInt, formatUsd } from '../api'

type OrderType = '1-tap' | 'market' | 'limit'

type Props = {
  tradeAction: 'BUY' | 'SELL'
  onTradeAction: (a: 'BUY' | 'SELL') => void
  side: 'UP' | 'DOWN'
  onSide: (s: 'UP' | 'DOWN') => void
  upPrice: number
  downPrice: number
  /** False when that outcome has no ask liquidity (cannot buy). */
  upHasAsk?: boolean
  downHasAsk?: boolean
  /** False when that outcome has no bid liquidity (cannot sell). */
  upHasBid?: boolean
  downHasBid?: boolean
  cash?: number
  heldShares?: number
  onTrade: (opts: { size_usd?: number; shares?: number }) => void
  tradeDisabled: boolean
  monitorHint?: boolean
}

const TAP_AMOUNTS = [5, 25, 100] as const
const MARKET_ADDS = [1, 5, 10, 100] as const
const SHARE_DELTAS = [-100, -10, 10, 50, 100] as const

const ORDER_LABELS: Record<OrderType, string> = {
  '1-tap': '1-Tap',
  market: 'Market',
  limit: 'Limit',
}

function cents(p: number): string {
  return formatCentsInt(p)
}

function winFromUsd(usd: number, price: number): number {
  if (price <= 0) return 0
  return usd / price
}

export default function TradeCard({
  tradeAction,
  onTradeAction,
  side,
  onSide,
  upPrice,
  downPrice,
  upHasAsk = true,
  downHasAsk = true,
  upHasBid = true,
  downHasBid = true,
  heldShares = 0,
  onTrade,
  tradeDisabled,
  monitorHint,
}: Props) {
  const [orderType, setOrderType] = useState<OrderType>('1-tap')
  const [orderMenuOpen, setOrderMenuOpen] = useState(false)
  const [marketAmount, setMarketAmount] = useState(0)
  const [limitCents, setLimitCents] = useState(Math.round((side === 'UP' ? upPrice : downPrice) * 100))
  const [shares, setShares] = useState(0)
  const orderMenuRef = useRef<HTMLDivElement>(null)

  const price = side === 'UP' ? upPrice : downPrice
  // Buy quotes from props; sell is always 1¢ lower
  const upBuy = upPrice
  const downBuy = downPrice
  const upSell = Math.max(0.000001, upBuy - 0.01)
  const downSell = Math.max(0.000001, downBuy - 0.01)
  const displayUp =
    tradeAction === 'SELL'
      ? upHasBid
        ? cents(upSell)
        : '--'
      : upHasAsk
        ? cents(upBuy)
        : '--'
  const displayDown =
    tradeAction === 'SELL'
      ? downHasBid
        ? cents(downSell)
        : '--'
      : downHasAsk
        ? cents(downBuy)
        : '--'
  const limitPrice = Math.max(0, Math.min(100, limitCents)) / 100
  const quotePrice = tradeAction === 'SELL' ? (side === 'UP' ? upSell : downSell) : price
  const fillPrice = orderType === 'limit' && limitPrice > 0 ? limitPrice : quotePrice

  const noLiquidity =
    tradeAction === 'BUY'
      ? side === 'UP'
        ? !upHasAsk
        : !downHasAsk
      : side === 'UP'
        ? !upHasBid
        : !downHasBid
  const disabled = tradeDisabled || noLiquidity

  const limitTotal = shares * fillPrice
  const limitToWin = tradeAction === 'BUY' ? shares * 1 : shares * fillPrice

  const orderOptions: OrderType[] =
    tradeAction === 'BUY' ? ['1-tap', 'market', 'limit'] : ['market', 'limit']

  useEffect(() => {
    const px = tradeAction === 'SELL' ? (side === 'UP' ? upSell : downSell) : side === 'UP' ? upBuy : downBuy
    setLimitCents(Math.round(px * 100))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [side, tradeAction])

  useEffect(() => {
    if (tradeAction === 'SELL' && orderType === '1-tap') {
      setOrderType('market')
    }
  }, [tradeAction, orderType])

  useEffect(() => {
    if (!orderMenuOpen) return
    const onDoc = (e: MouseEvent) => {
      if (!orderMenuRef.current?.contains(e.target as Node)) {
        setOrderMenuOpen(false)
      }
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOrderMenuOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDoc)
      document.removeEventListener('keydown', onKey)
    }
  }, [orderMenuOpen])

  const bumpShares = (delta: number) => {
    setShares((s) => Math.max(0, Math.round((s + delta) * 100) / 100))
  }

  const submit = () => {
    if (orderType === '1-tap') return
    if (orderType === 'market') {
      if (marketAmount <= 0) return
      onTrade({ size_usd: marketAmount })
      return
    }
    if (shares <= 0) return
    onTrade({ shares })
  }

  const canSubmit =
    !disabled &&
    ((orderType === 'market' && marketAmount > 0) || (orderType === 'limit' && shares > 0))

  return (
    <div className="trade-card">
      <div className="trade-card-head">
        <div className="trade-card-logo" aria-hidden>
          ₿
        </div>
        <div>
          <div className="trade-card-title">BTC Up or Down 5m</div>
          <div className={`trade-card-side ${side === 'UP' ? 'up' : 'down'}`}>
            {side === 'UP' ? 'Up' : 'Down'}
          </div>
        </div>
      </div>

      {monitorHint && (
        <p className="sidebar-hint" style={{ marginTop: '0.65rem' }}>
          Switch to Paper to place simulated orders.
        </p>
      )}

      <div className="trade-tabs-row">
        <div className="trade-tabs">
          <button
            type="button"
            className={tradeAction === 'BUY' ? 'active' : ''}
            onClick={() => onTradeAction('BUY')}
          >
            Buy
          </button>
          <button
            type="button"
            className={tradeAction === 'SELL' ? 'active' : ''}
            onClick={() => onTradeAction('SELL')}
          >
            Sell
          </button>
        </div>
        <div className="trade-order-menu" ref={orderMenuRef}>
          <button
            type="button"
            className={`trade-order-trigger ${orderMenuOpen ? 'open' : ''}`}
            aria-haspopup="listbox"
            aria-expanded={orderMenuOpen}
            onClick={() => setOrderMenuOpen((o) => !o)}
          >
            {ORDER_LABELS[orderType]}
            <span className="trade-order-chevron" aria-hidden>
              ▾
            </span>
          </button>
          {orderMenuOpen && (
            <ul className="trade-order-dropdown" role="listbox">
              {orderOptions.map((opt) => (
                <li key={opt} role="option" aria-selected={orderType === opt}>
                  <button
                    type="button"
                    className={orderType === opt ? 'active' : ''}
                    onClick={() => {
                      setOrderType(opt)
                      setOrderMenuOpen(false)
                    }}
                  >
                    {ORDER_LABELS[opt]}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>

      <div className={`trade-outcomes${disabled ? ' trade-outcomes-disabled' : ''}`}>
        <button
          type="button"
          className={`trade-outcome up ${side === 'UP' ? 'active' : ''}`}
          onClick={() => onSide('UP')}
          disabled={disabled && side === 'UP'}
        >
          Up {displayUp}
        </button>
        <button
          type="button"
          className={`trade-outcome down ${side === 'DOWN' ? 'active' : ''}`}
          onClick={() => onSide('DOWN')}
          disabled={disabled && side === 'DOWN'}
        >
          Down {displayDown}
        </button>
      </div>

      {orderType === '1-tap' && (
        <div className="trade-onetap">
          <div className="trade-onetap-label">
            {tradeAction === 'BUY' ? 'One-tap buy' : 'One-tap sell'}
          </div>
          <div className="trade-onetap-grid">
            {TAP_AMOUNTS.map((usd) => {
              const win = winFromUsd(usd, fillPrice)
              return (
                <button
                  key={usd}
                  type="button"
                  disabled={tradeDisabled}
                  onClick={() => onTrade({ size_usd: usd })}
                >
                  <strong>${usd}</strong>
                  <span>win ${formatUsd(win, win >= 10 ? 0 : 2)}</span>
                </button>
              )
            })}
          </div>
        </div>
      )}

      {orderType === 'market' && (
        <div className="trade-market">
          <div className="trade-amount-row">
            <span>Amount</span>
            <input
              className="trade-amount-input"
              type="number"
              min={0}
              step={1}
              value={marketAmount || ''}
              placeholder="$0"
              onChange={(e) => setMarketAmount(Math.max(0, Number(e.target.value) || 0))}
            />
          </div>
          <div className="trade-add-row">
            {MARKET_ADDS.map((n) => (
              <button
                key={n}
                type="button"
                onClick={() => setMarketAmount((a) => a + n)}
              >
                +${n}
              </button>
            ))}
          </div>
          <button type="button" className="trade-submit" disabled={!canSubmit} onClick={submit}>
            Trade
          </button>
        </div>
      )}

      {orderType === 'limit' && (
        <div className="trade-limit">
          <div className="trade-field-row">
            <span>Limit price</span>
            <div className="trade-stepper">
              <button
                type="button"
                onClick={() => setLimitCents((c) => Math.max(0, c - 1))}
              >
                −
              </button>
              <div className="trade-stepper-value">
                <input
                  type="number"
                  min={0}
                  max={100}
                  step={1}
                  value={limitCents}
                  onChange={(e) => setLimitCents(Number(e.target.value))}
                />
                <span>¢</span>
              </div>
              <button
                type="button"
                onClick={() => setLimitCents((c) => Math.min(100, c + 1))}
              >
                +
              </button>
            </div>
          </div>

          <div className="trade-field-row">
            <span>Shares</span>
            <input
              className="trade-shares-input"
              type="number"
              min={0}
              step={1}
              value={shares || ''}
              placeholder="0"
              onChange={(e) => setShares(Math.max(0, Number(e.target.value) || 0))}
            />
          </div>

          <div className="trade-share-deltas">
            {SHARE_DELTAS.map((d) => (
              <button
                key={d}
                type="button"
                className={d === 50 ? 'accent' : ''}
                onClick={() => bumpShares(d)}
              >
                {d > 0 ? `+${d}` : d}
              </button>
            ))}
          </div>

          <div className="trade-divider" />

          <div className="trade-summary-row">
            <span>Expires</span>
            <select defaultValue="never">
              <option value="never">Never</option>
              <option value="eow">End of window</option>
            </select>
          </div>

          <div className="trade-summary-row">
            <span>Total</span>
            <strong className="trade-total">${formatUsd(limitTotal, 2)}</strong>
          </div>

          <div className="trade-summary-row receive">
            <span>
              To win <span className="trade-info" title="Payout if this outcome wins">ⓘ</span>
            </span>
            <strong>${formatUsd(limitToWin, 2)}</strong>
          </div>

          <button type="button" className="trade-submit" disabled={!canSubmit} onClick={submit}>
            Trade
          </button>

          {tradeAction === 'SELL' && heldShares > 0 && (
            <div className="trade-held">Held: {heldShares.toFixed(2)} shares</div>
          )}
        </div>
      )}
    </div>
  )
}
