import { useState } from "react";
import { Link } from "react-router-dom";
import { useDeleteDecision } from "../hooks/useDecisions";
import DecisionForm from "./DecisionForm";

const HORIZONS = [21, 63, 126];

export function formatExcess(value) {
  if (value == null) return "—";
  const pct = Number(value) * 100;
  return `${pct > 0 ? "+" : pct < 0 ? "−" : ""}${Math.abs(pct).toFixed(1)}%`;
}

function excessClass(value) {
  if (value == null) return "text-light-brown";
  const v = Number(value);
  return v > 0 ? "text-gain" : v < 0 ? "text-loss" : "text-ink";
}

// One horizon cell: excess vs SPY, or why there isn't one yet.
export function OutcomeCell({ outcome }) {
  if (!outcome) {
    return <span className="text-light-brown" title="Not due yet">·</span>;
  }
  if (outcome.status === "no_data") {
    return (
      <span className="text-light-brown" title="No price data for this ticker; excluded from stats">
        n/a
      </span>
    );
  }
  const truncated = outcome.status === "truncated";
  return (
    <span
      className={excessClass(outcome.excess_return)}
      title={`${truncated ? "Truncated (data ended " : "Exit "}${outcome.exit_date}${
        truncated ? ")" : ""
      }: ticker ${formatExcess(outcome.ticker_return)}, SPY ${formatExcess(outcome.spy_return)}`}
    >
      {formatExcess(outcome.excess_return)}
      {truncated && <sup className="text-mid-brown">T</sup>}
    </span>
  );
}

function CallBadge({ decision }) {
  return (
    <span
      className={`inline-block text-xs px-2 py-0.5 border border-ink uppercase tracking-wide font-medium text-warm-white ${
        decision === "buy" ? "bg-success" : "bg-mid-brown"
      }`}
    >
      {decision}
    </span>
  );
}

function formatDate(iso) {
  return new Date(iso).toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

/**
 * Decision log table. ``showTicker`` is off on the ticker page, where every
 * row is the same symbol.
 */
export default function DecisionList({ decisions, showTicker = true }) {
  const deleteDecision = useDeleteDecision();
  const [superseding, setSuperseding] = useState(null);

  if (!decisions || decisions.length === 0) {
    return <div className="text-sm text-mid-brown italic">No decisions recorded yet.</div>;
  }

  const now = Date.now();

  return (
    <>
      <div className="border-2 border-ink bg-warm-white overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b-2 border-ink bg-cream text-xs uppercase tracking-wide text-dark-brown">
              <th className="text-left py-2 px-3 font-medium">Date</th>
              {showTicker && <th className="text-left py-2 px-3 font-medium">Ticker</th>}
              <th className="text-left py-2 px-3 font-medium">Call</th>
              <th className="text-left py-2 px-3 font-medium">Conv.</th>
              <th className="text-left py-2 px-3 font-medium">Price</th>
              {HORIZONS.map((h) => (
                <th key={h} className="text-right py-2 px-3 font-medium" title={`Excess return vs SPY after ${h} sessions`}>
                  {h}d vs SPY
                </th>
              ))}
              <th className="text-left py-2 px-3 font-medium">Thesis / wrong if</th>
              <th className="py-2 px-3" />
            </tr>
          </thead>
          <tbody>
            {decisions.map((d) => {
              const byHorizon = Object.fromEntries(
                (d.outcomes || []).map((o) => [o.horizon_sessions, o])
              );
              const deletable = new Date(d.deletable_until).getTime() > now;
              return (
                <tr
                  key={d.id}
                  className={`border-b border-ink/20 align-top ${d.superseded_by_id ? "opacity-60" : ""}`}
                >
                  <td className="py-2 px-3 whitespace-nowrap text-mid-brown">
                    {formatDate(d.created_at)}
                  </td>
                  {showTicker && (
                    <td className="py-2 px-3">
                      <Link to={`/ticker/${d.ticker}`} className="font-mono font-bold text-accent hover:underline">
                        {d.ticker}
                      </Link>
                    </td>
                  )}
                  <td className="py-2 px-3">
                    <CallBadge decision={d.decision} />
                  </td>
                  <td className="py-2 px-3 font-mono">{d.conviction}</td>
                  <td className="py-2 px-3 font-mono whitespace-nowrap">
                    ${Number(d.price_at_decision).toFixed(2)}
                  </td>
                  {HORIZONS.map((h) => (
                    <td key={h} className="py-2 px-3 font-mono text-right whitespace-nowrap">
                      <OutcomeCell outcome={byHorizon[h]} />
                    </td>
                  ))}
                  <td className="py-2 px-3 max-w-md">
                    <div className="text-ink">{d.thesis}</div>
                    <div className="text-xs text-mid-brown">Wrong if: {d.invalidation}</div>
                    {d.superseded_by_id && (
                      <div className="text-xs text-mid-brown italic">Superseded by a later call</div>
                    )}
                    {d.supersedes_id && (
                      <div className="text-xs text-mid-brown italic">Replaces an earlier call</div>
                    )}
                  </td>
                  <td className="py-2 px-3 whitespace-nowrap text-right">
                    {deletable ? (
                      <button
                        type="button"
                        onClick={() => deleteDecision.mutate(d.id)}
                        disabled={deleteDecision.isPending}
                        className="text-xs text-light-brown hover:text-error"
                        title="Deletable for 15 minutes after recording"
                      >
                        Delete
                      </button>
                    ) : (
                      !d.superseded_by_id && (
                        <button
                          type="button"
                          onClick={() => setSuperseding(d)}
                          className="text-xs text-mid-brown hover:text-accent"
                          title="Record a new call that replaces this one"
                        >
                          Change call
                        </button>
                      )
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {deleteDecision.isError && (
        <p className="mt-2 text-sm text-error">{deleteDecision.error.message}</p>
      )}
      {superseding && (
        <DecisionForm
          ticker={superseding.ticker}
          source="manual"
          supersedes={superseding}
          onClose={() => setSuperseding(null)}
        />
      )}
    </>
  );
}
