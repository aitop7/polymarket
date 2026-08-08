import { formatUsd } from '../api'

type Props = {
  title?: string
  marketId?: string
  windowLabel: string
  priceToBeat: number | null | undefined
  /** Chainlink 30s TWAP (resolution feed) */
  twapPrice: number | null | undefined
  remainingSeconds: number | null | undefined
}

function pad2(n: number): string {
  return String(Math.max(0, Math.floor(n))).padStart(2, '0')
}

export default function BtcPricePanel({
  title = 'BTC Up or Down 5m',
  marketId,
  windowLabel,
  priceToBeat,
  twapPrice,
  remainingSeconds,
}: Props) {
  const delta =
    twapPrice != null && priceToBeat != null ? twapPrice - priceToBeat : null
  const above = delta != null && delta >= 0
  const rem = remainingSeconds != null ? Math.max(0, remainingSeconds) : null
  const mins = rem != null ? Math.floor(rem / 60) : null
  const secs = rem != null ? Math.floor(rem % 60) : null

  return (
    <section className="btc-panel">
      <div className="btc-panel-sticky">
        <div className="btc-panel-identity">
          <div className="btc-logo" aria-hidden>
            ₿
          </div>
          <div>
            <h1 className="btc-panel-title">
              {title}
              {marketId ? <span className="btc-market-id">({marketId})</span> : null}
            </h1>
            <div className="btc-panel-sub">{windowLabel}</div>
          </div>
        </div>
      </div>

      <div className="btc-panel-stats">
        <div className="btc-stat">
          <div className="btc-stat-label">Price To Beat</div>
          <div className="btc-stat-hint">TWAP at window start</div>
          <div className="btc-stat-value beat">${formatUsd(priceToBeat, 2)}</div>
        </div>

        <div className="btc-stat">
          <div className="btc-stat-label-row">
            <span className="btc-stat-label current-label">Chainlink 30s TWAP</span>
            {delta != null && (
              <span className={`btc-delta ${above ? 'up' : 'down'}`}>
                {above ? '▲' : '▼'} $
                {formatUsd(Math.abs(delta), Math.abs(delta) < 1 ? 1 : 0)}
              </span>
            )}
          </div>
          <div className={`btc-stat-value current ${above ? 'up' : 'down'}`}>
            {twapPrice == null ? (
              <span className="btc-waiting">Waiting for TWAP…</span>
            ) : (
              `$${formatUsd(twapPrice, 2)}`
            )}
          </div>
        </div>

        <div className="btc-countdown" aria-label="Time remaining">
          {mins != null && secs != null ? (
            <div className="btc-countdown-digits">
              <div className="btc-countdown-block">
                <span className="btc-countdown-num">{pad2(mins)}</span>
                <span className="btc-countdown-unit">MINS</span>
              </div>
              <div className="btc-countdown-block">
                <span className="btc-countdown-num">{pad2(secs)}</span>
                <span className="btc-countdown-unit">SECS</span>
              </div>
            </div>
          ) : (
            <div className="btc-countdown-digits muted-countdown">
              <div className="btc-countdown-block">
                <span className="btc-countdown-num">--</span>
                <span className="btc-countdown-unit">MINS</span>
              </div>
              <div className="btc-countdown-block">
                <span className="btc-countdown-num">--</span>
                <span className="btc-countdown-unit">SECS</span>
              </div>
            </div>
          )}
        </div>
      </div>
    </section>
  )
}
