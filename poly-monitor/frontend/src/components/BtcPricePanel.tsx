import { formatResolvedEt, formatUsd } from '../api'

type Props = {
  title?: string
  marketId?: string
  windowLabel: string
  priceToBeat: number | null | undefined
  /** Chainlink 30s TWAP (resolution feed) */
  twapPrice: number | null | undefined
  /** Live RTDS error when Current Price has no TWAP sample */
  twapError?: string | null
  remainingSeconds: number | null | undefined
  /** History: resolved outcome from meta */
  outcome?: 'Up' | 'Down' | 'not_closed' | null
  resolvedAt?: number | null
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
  twapError = null,
  remainingSeconds,
  outcome = null,
  resolvedAt = null,
}: Props) {
  const delta =
    twapPrice != null && priceToBeat != null ? twapPrice - priceToBeat : null
  const above = delta != null && delta >= 0
  const rem = remainingSeconds != null ? Math.max(0, remainingSeconds) : null
  const mins = rem != null ? Math.floor(rem / 60) : null
  const secs = rem != null ? Math.floor(rem % 60) : null
  const resolved = outcome === 'Up' || outcome === 'Down'
  const resolvedLabel =
    resolvedAt != null && Number.isFinite(resolvedAt) ? formatResolvedEt(resolvedAt) : ''

  return (
    <section className="btc-panel">
      <div className="btc-panel-sticky">
        <div className="btc-panel-identity">
          <div className="btc-logo" aria-hidden>
            ₿
          </div>
          <div className="btc-panel-identity-text">
            <h1 className="btc-panel-title">
              {title}
              {marketId ? <span className="btc-market-id">({marketId})</span> : null}
            </h1>
            <div className="btc-panel-sub">{windowLabel}</div>
          </div>
        </div>
        {outcome != null && (
          <div
            className={`btc-panel-outcome ${resolved ? (outcome === 'Up' ? 'up' : 'down') : 'pending'}`}
          >
            <div className="btc-outcome-label">Outcome</div>
            <div className="btc-outcome-value">
              {resolved ? (
                <>
                  <span className="btc-outcome-arrow" aria-hidden>
                    {outcome === 'Up' ? '▲' : '▼'}
                  </span>
                  {outcome}
                </>
              ) : (
                'Not closed'
              )}
            </div>
            {resolved && resolvedLabel ? (
              <div className="btc-resolved-at">Resolved {resolvedLabel}</div>
            ) : null}
          </div>
        )}
      </div>

      <div className="btc-panel-stats">
        <div className="btc-stat">
          <div className="btc-stat-label">Price To Beat</div>
          <div className="btc-stat-hint">Polymarket strike</div>
          <div className="btc-stat-value beat">${formatUsd(priceToBeat, 2)}</div>
        </div>

        <div className="btc-stat">
          <div className="btc-stat-label-row">
            <span className="btc-stat-label current-label">Current Price</span>
            {delta != null && (
              <span className={`btc-delta ${above ? 'up' : 'down'}`}>
                {above ? '▲' : '▼'} $
                {formatUsd(Math.abs(delta), Math.abs(delta) >= 1 ? 0 : 2)}
              </span>
            )}
          </div>
          <div className={`btc-stat-value current ${above ? 'up' : 'down'}`}>
            {twapPrice == null ? (
              <span className="btc-waiting">—</span>
            ) : (
              `$${formatUsd(twapPrice, 2)}`
            )}
          </div>
          {twapPrice == null && twapError ? (
            <div className="btc-stat-hint btc-twap-error" title={twapError}>
              TWAP feed: {twapError.length > 64 ? `${twapError.slice(0, 64)}…` : twapError}
            </div>
          ) : null}
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
