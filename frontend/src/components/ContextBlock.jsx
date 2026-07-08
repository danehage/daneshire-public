import { useTickerContext, useTickerNarrative } from "../hooks/useTicker";

function formatRevenue(revenue) {
  if (revenue >= 1_000_000_000) return `$${(revenue / 1_000_000_000).toFixed(1)}B`;
  if (revenue >= 1_000_000) return `$${(revenue / 1_000_000).toFixed(0)}M`;
  return `$${revenue.toLocaleString()}`;
}

function Arrow({ pct }) {
  if (pct === null || pct === undefined) return null;
  const positive = pct >= 0;
  return (
    <span className={`text-xs font-medium ml-1 ${positive ? "text-success" : "text-error"}`}>
      {positive ? "▲" : "▼"} {Math.abs(pct).toFixed(1)}%
    </span>
  );
}

function RevenueRow({ q }) {
  return (
    <div className="flex items-baseline justify-between py-1.5 border-b border-light-brown last:border-b-0">
      <span className="text-xs text-mid-brown w-16 shrink-0">{q.quarter}</span>
      <div className="text-right">
        <span className="font-mono text-sm text-ink">
          {q.revenue ? formatRevenue(q.revenue) : "—"}
        </span>
        <Arrow pct={q.revenue_pct_change} />
        <span className="text-xs text-light-brown ml-2">
          {q.eps != null ? `EPS $${q.eps.toFixed(2)}` : ""}
          {q.eps != null && <Arrow pct={q.eps_pct_change} />}
        </span>
      </div>
    </div>
  );
}

export default function ContextBlock({ ticker }) {
  const { data: ctx, isLoading: ctxLoading, error: ctxError } = useTickerContext(ticker);
  const { data: narr, isLoading: narrLoading } = useTickerNarrative(ticker);

  if (ctxLoading) {
    return (
      <div className="border-2 border-ink shadow-hard bg-warm-white p-6">
        <div className="text-xs font-medium uppercase tracking-wide text-dark-brown mb-3">
          Context
        </div>
        <div className="text-sm text-light-brown animate-pulse">Loading context…</div>
      </div>
    );
  }

  if (ctxError || !ctx) {
    return null;
  }

  return (
    <div className="border-2 border-ink shadow-hard bg-warm-white p-6 space-y-5">
      {/* Header row */}
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="text-xs font-medium uppercase tracking-wide text-dark-brown mb-1">
            Context
          </h2>
          {ctx.company_name && (
            <p className="font-serif text-lg text-ink">
              {ctx.company_name}
              {ctx.sector && (
                <span className="font-sans text-sm text-mid-brown ml-2">· {ctx.sector}</span>
              )}
            </p>
          )}
        </div>
        {ctx.drawdown_from_52w_high_pct != null && (
          <div className="text-right shrink-0">
            <div className="text-xs text-mid-brown mb-0.5">From 52w high</div>
            <div className="font-mono text-lg text-error font-semibold">
              −{ctx.drawdown_from_52w_high_pct.toFixed(1)}%
            </div>
            {ctx.high_52w && (
              <div className="text-xs text-light-brown">${ctx.high_52w.toFixed(2)}</div>
            )}
          </div>
        )}
      </div>

      {/* Narrative */}
      <div className="text-sm text-ink leading-relaxed min-h-[3rem]">
        {narrLoading ? (
          <span className="text-light-brown italic animate-pulse">Analyzing…</span>
        ) : narr?.narrative ? (
          <p>{narr.narrative}</p>
        ) : null}
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        {/* Revenue + EPS (last 4Q) */}
        {ctx.quarterly_financials?.length > 0 && (
          <div>
            <div className="text-xs font-medium uppercase tracking-wide text-dark-brown mb-2">
              Revenue / EPS (last 4Q)
            </div>
            <div>
              {ctx.quarterly_financials.map((q) => (
                <RevenueRow key={q.quarter} q={q} />
              ))}
            </div>
          </div>
        )}

        {/* Fundamentals */}
        <div className="space-y-2">
          {ctx.net_margin_ttm != null && (
            <div className="flex justify-between">
              <span className="text-sm text-mid-brown">Net margin (TTM)</span>
              <span className="font-mono text-sm text-ink">{ctx.net_margin_ttm.toFixed(1)}%</span>
            </div>
          )}
          {ctx.debt_to_equity != null && (
            <div className="flex justify-between">
              <span className="text-sm text-mid-brown">Debt / equity</span>
              <span className="font-mono text-sm text-ink">{ctx.debt_to_equity.toFixed(2)}</span>
            </div>
          )}
          {ctx.next_earnings_date && (
            <div className="flex justify-between">
              <span className="text-sm text-mid-brown">Next earnings</span>
              <span className="font-mono text-sm text-ink">
                {ctx.next_earnings_date}
                {ctx.days_to_earnings != null && (
                  <span className="text-light-brown ml-1">({ctx.days_to_earnings}d)</span>
                )}
              </span>
            </div>
          )}
        </div>
      </div>

      {/* Headlines */}
      {ctx.headlines?.length > 0 && (
        <div>
          <div className="text-xs font-medium uppercase tracking-wide text-dark-brown mb-2">
            Recent headlines
          </div>
          <ul className="space-y-1.5">
            {ctx.headlines.map((h, i) => (
              <li key={i} className="text-sm">
                <a
                  href={h.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-accent hover:underline"
                >
                  {h.title}
                </a>
                {h.source && (
                  <span className="text-light-brown ml-2 text-xs">{h.source}</span>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
