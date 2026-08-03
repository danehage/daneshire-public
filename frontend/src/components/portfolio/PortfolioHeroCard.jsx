import Card from "../Card";
import { useShareMode, MASKED_MONEY } from "../../hooks/useShareMode";

function freshnessLabel(lastSnapshotAt) {
  if (!lastSnapshotAt) return null;
  const snapshotDate = new Date(lastSnapshotAt);
  const now = new Date();
  const diffMs = now - snapshotDate;
  const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24));
  if (diffDays <= 0) return "snapshot from today";
  if (diffDays === 1) return "snapshot from 1 day ago";
  return `snapshot from ${diffDays} days ago`;
}

function fmt(value) {
  if (value == null) return "—";
  const n = Number(value);
  return n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

export default function PortfolioHeroCard({ portfolio }) {
  const { shareMode } = useShareMode();
  if (!portfolio) return null;

  const { total_value, day_change, cash_balance, last_snapshot_at } = portfolio;
  const dayChangeNum = day_change != null ? Number(day_change) : null;
  const isPositive = dayChangeNum != null && dayChangeNum > 0;
  const isNegative = dayChangeNum != null && dayChangeNum < 0;
  const freshness = freshnessLabel(last_snapshot_at);

  return (
    <Card className="mb-6">
      <div className="px-4 py-3 border-b-2 border-ink flex items-center justify-between">
        <h2 className="font-serif font-bold text-lg">Portfolio Value</h2>
        {freshness && (
          <span className="text-xs font-mono text-mid-brown">{freshness}</span>
        )}
      </div>
      <div className="grid grid-cols-3 divide-x-2 divide-ink">
        <div className="px-6 py-4">
          <div className="text-xs font-mono font-bold uppercase tracking-wide text-mid-brown mb-1">
            Total Value
          </div>
          <div className="text-2xl font-serif font-bold text-ink">
            {shareMode ? MASKED_MONEY : `$${fmt(total_value)}`}
          </div>
        </div>
        <div className="px-6 py-4">
          <div className="text-xs font-mono font-bold uppercase tracking-wide text-mid-brown mb-1">
            Day Change
          </div>
          <div
            className={`text-2xl font-serif font-bold ${
              isPositive ? "text-gain" : isNegative ? "text-loss" : "text-ink"
            }`}
          >
            {dayChangeNum != null
              ? shareMode
                ? `${dayChangeNum >= 0 ? "+" : "−"}${MASKED_MONEY}`
                : `${dayChangeNum >= 0 ? "+" : ""}$${fmt(day_change)}`
              : "—"}
          </div>
        </div>
        <div className="px-6 py-4">
          <div className="text-xs font-mono font-bold uppercase tracking-wide text-mid-brown mb-1">
            Cash
          </div>
          <div className="text-2xl font-serif font-bold text-ink">
            {cash_balance != null ? (shareMode ? MASKED_MONEY : `$${fmt(cash_balance)}`) : "—"}
          </div>
        </div>
      </div>
    </Card>
  );
}
