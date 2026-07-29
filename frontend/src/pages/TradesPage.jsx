import { Link } from "react-router-dom";
import { useTradeCampaigns } from "../hooks/useTradeCampaigns";

function money(value) {
  if (value === null || value === undefined) return "—";
  const n = Number(value);
  return n.toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
  });
}

function PnlCell({ value }) {
  if (value === null || value === undefined) {
    return <span className="text-mid-brown">—</span>;
  }
  const n = Number(value);
  return (
    <span className={`font-mono ${n >= 0 ? "text-gain" : "text-loss"}`}>
      {n >= 0 ? "+" : ""}
      {money(n)}
    </span>
  );
}

function CampaignTable({ campaigns, showClosed }) {
  if (campaigns.length === 0) {
    return (
      <p className="text-sm text-mid-brown py-4">
        {showClosed ? "No closed campaigns synced yet." : "No open campaigns synced yet."}
      </p>
    );
  }
  return (
    <table className="w-full text-sm">
      <thead>
        <tr className="border-b-2 border-ink text-left">
          <th className="py-2 px-3 font-bold uppercase text-xs tracking-wide">Ticker</th>
          <th className="py-2 px-3 font-bold uppercase text-xs tracking-wide">Campaign</th>
          <th className="py-2 px-3 font-bold uppercase text-xs tracking-wide">Opened</th>
          {showClosed && (
            <th className="py-2 px-3 font-bold uppercase text-xs tracking-wide">Closed</th>
          )}
          <th className="py-2 px-3 font-bold uppercase text-xs tracking-wide text-right">Cost basis</th>
          {showClosed && (
            <th className="py-2 px-3 font-bold uppercase text-xs tracking-wide text-right">Realized P/L</th>
          )}
          <th className="py-2 px-3 font-bold uppercase text-xs tracking-wide">Account</th>
        </tr>
      </thead>
      <tbody>
        {campaigns.map((c) => (
          <tr key={c.id} className="border-b border-ink/20 hover:bg-warm-white">
            <td className="py-2 px-3 font-mono font-bold">
              <Link to={`/ticker/${c.ticker}`} className="hover:underline">
                {c.ticker}
              </Link>
            </td>
            <td className="py-2 px-3">
              <Link to={`/trades/${c.id}`} className="font-mono hover:underline">
                {c.title}
              </Link>
              {c.parse_error && (
                <span
                  className="ml-2 text-xs text-warning"
                  title={c.parse_error}
                >
                  ⚠ parse
                </span>
              )}
            </td>
            <td className="py-2 px-3 font-mono">{c.opened_on ?? "—"}</td>
            {showClosed && <td className="py-2 px-3 font-mono">{c.closed_on ?? "—"}</td>}
            <td className="py-2 px-3 font-mono text-right">
              {c.cost_basis !== null && c.cost_basis !== undefined
                ? `$${Number(c.cost_basis).toFixed(2)}`
                : "—"}
            </td>
            {showClosed && (
              <td className="py-2 px-3 text-right">
                <PnlCell value={c.realized_pnl} />
              </td>
            )}
            <td className="py-2 px-3 font-mono text-xs uppercase">{c.account ?? "—"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export default function TradesPage() {
  const { data: campaigns, isLoading, error } = useTradeCampaigns();

  if (isLoading) {
    return <p className="text-sm text-mid-brown p-6">Loading trade campaigns…</p>;
  }
  if (error) {
    return <p className="text-sm text-error p-6">Failed to load campaigns: {error.message}</p>;
  }

  const open = campaigns.filter((c) => c.status === "open");
  const closed = campaigns.filter((c) => c.status === "closed");
  const lastSynced = campaigns.length
    ? campaigns.reduce((max, c) => (c.synced_at > max ? c.synced_at : max), campaigns[0].synced_at)
    : null;

  return (
    <div className="max-w-5xl mx-auto p-6 space-y-8">
      <div className="flex items-baseline justify-between">
        <h1 className="text-2xl font-bold">Trades</h1>
        <p className="text-xs text-mid-brown font-mono">
          Journaled in the DaneBrain vault · one-way sync
          {lastSynced && ` · last synced ${new Date(lastSynced).toLocaleString()}`}
        </p>
      </div>

      <section>
        <h2 className="text-lg font-bold mb-2">
          Open <span className="font-mono text-mid-brown">({open.length})</span>
        </h2>
        <CampaignTable campaigns={open} showClosed={false} />
      </section>

      <section>
        <h2 className="text-lg font-bold mb-2">
          Closed <span className="font-mono text-mid-brown">({closed.length})</span>
        </h2>
        <CampaignTable campaigns={closed} showClosed={true} />
      </section>
    </div>
  );
}
