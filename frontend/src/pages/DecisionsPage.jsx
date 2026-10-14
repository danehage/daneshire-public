import { useState } from "react";
import { useDecisions, useDecisionSummary } from "../hooks/useDecisions";
import DecisionList, { formatExcess } from "../components/DecisionList";

function SummaryCell({ cell, minN }) {
  if (!cell || cell.n === 0) {
    return <td className="py-2 px-3 text-center text-light-brown">—</td>;
  }
  const thin = cell.n < minN;
  const mean = cell.mean_excess_return;
  return (
    <td
      className={`py-2 px-3 text-center font-mono ${thin ? "text-light-brown bg-cream" : ""}`}
      title={
        thin
          ? `Only ${cell.n} scored; too few to read anything into`
          : `median ${formatExcess(cell.median_excess_return)}`
      }
    >
      <div className={thin ? "" : mean > 0 ? "text-gain" : mean < 0 ? "text-loss" : "text-ink"}>
        {formatExcess(mean)}
      </div>
      <div className="text-xs">
        n={cell.n} · hit {Math.round(cell.hit_rate * 100)}%
      </div>
    </td>
  );
}

function SummaryGrid({ summary }) {
  const cells = Object.fromEntries(
    summary.cells.map((c) => [`${c.decision}-${c.conviction}-${c.horizon_sessions}`, c])
  );
  const rows = [];
  for (const call of ["buy", "pass"]) {
    for (let conviction = 5; conviction >= 1; conviction -= 1) {
      rows.push({ call, conviction });
    }
  }

  return (
    <div className="border-2 border-ink shadow-hard bg-warm-white overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b-2 border-ink bg-cream text-xs uppercase tracking-wide text-dark-brown">
            <th className="text-left py-2 px-3 font-medium">Call</th>
            <th className="text-left py-2 px-3 font-medium">Conviction</th>
            {summary.horizons.map((h) => (
              <th key={h} className="text-center py-2 px-3 font-medium">
                {h} sessions
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map(({ call, conviction }) => (
            <tr
              key={`${call}-${conviction}`}
              className={`border-b border-ink/20 ${call === "pass" && conviction === 5 ? "border-t-2 border-t-ink" : ""}`}
            >
              <td className="py-2 px-3 uppercase text-xs font-medium tracking-wide">{call}</td>
              <td className="py-2 px-3 font-mono">{conviction}</td>
              {summary.horizons.map((h) => (
                <SummaryCell
                  key={h}
                  cell={cells[`${call}-${conviction}-${h}`]}
                  minN={summary.min_reliable_n}
                />
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function DecisionsPage() {
  const [callFilter, setCallFilter] = useState("");
  const { data: summary, isLoading: summaryLoading, error: summaryError } = useDecisionSummary();
  const { data: decisions, isLoading, error } = useDecisions({
    decision: callFilter || undefined,
    limit: 500,
  });

  return (
    <div className="space-y-8">
      <div>
        <h1 className="font-serif text-4xl text-ink mb-2">Decisions</h1>
        <p className="text-mid-brown max-w-3xl">
          Every buy/pass call, frozen when it was made and scored against SPY
          after 21, 63 and 126 trading sessions. Cells show mean excess return
          vs SPY, the count and the hit rate (share that beat SPY). Passes are
          not flipped: a positive number on a pass is a winner you passed on.
          Superseded calls count; cells with fewer than{" "}
          {summary?.min_reliable_n ?? 10} calls are greyed out.
        </p>
      </div>

      <section className="space-y-3">
        <h2 className="font-serif text-2xl text-ink">
          Results{summary ? ` (${summary.total_decisions} decisions)` : ""}
        </h2>
        {summaryLoading && <div className="text-mid-brown">Loading...</div>}
        {summaryError && <div className="text-error">{summaryError.message}</div>}
        {summary && <SummaryGrid summary={summary} />}
      </section>

      <section className="space-y-3">
        <div className="flex items-center justify-between">
          <h2 className="font-serif text-2xl text-ink">Log</h2>
          <select
            value={callFilter}
            onChange={(e) => setCallFilter(e.target.value)}
            className="px-3 py-2 border-2 border-ink bg-warm-white text-sm"
          >
            <option value="">All calls</option>
            <option value="buy">Buys</option>
            <option value="pass">Passes</option>
          </select>
        </div>
        {isLoading && <div className="text-mid-brown">Loading...</div>}
        {error && <div className="text-error">{error.message}</div>}
        {decisions && <DecisionList decisions={decisions} />}
        <p className="text-xs text-mid-brown">
          Record a call from a scanner row (Decide) or a ticker page (Record
          decision). <sup>T</sup> marks a truncated score: the ticker stopped
          trading before the horizon, so it was scored at its last close.
        </p>
      </section>
    </div>
  );
}
