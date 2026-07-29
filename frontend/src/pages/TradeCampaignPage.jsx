import { Link, useParams } from "react-router-dom";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useTradeCampaign } from "../hooks/useTradeCampaigns";

const STATUS_STYLES = {
  open: "border-success text-success bg-success/10",
  closed: "border-ink text-ink bg-warm-white",
  archived: "border-ink/40 text-mid-brown",
};

function Fact({ label, children }) {
  return (
    <div>
      <div className="text-xs uppercase tracking-wide text-mid-brown">{label}</div>
      <div className="font-mono text-sm">{children ?? "—"}</div>
    </div>
  );
}

export default function TradeCampaignPage() {
  const { campaignId } = useParams();
  const { data: campaign, isLoading, error } = useTradeCampaign(campaignId);

  if (isLoading) {
    return <p className="text-sm text-mid-brown p-6">Loading campaign…</p>;
  }
  if (error) {
    return <p className="text-sm text-error p-6">Failed to load campaign: {error.message}</p>;
  }

  const pnl = campaign.realized_pnl !== null && campaign.realized_pnl !== undefined
    ? Number(campaign.realized_pnl)
    : null;

  return (
    <div className="max-w-4xl mx-auto p-6 space-y-6">
      <div className="flex items-center gap-3 text-sm">
        <Link to="/trades" className="font-mono text-mid-brown hover:underline">
          ← Trades
        </Link>
      </div>

      <div className="border-2 border-ink bg-warm-white p-4 space-y-3">
        <div className="flex items-center gap-3 flex-wrap">
          <h1 className="text-2xl font-bold font-mono">
            <Link to={`/ticker/${campaign.ticker}`} className="hover:underline">
              {campaign.ticker}
            </Link>
          </h1>
          <span className="font-mono text-mid-brown">{campaign.title}</span>
          <span
            className={`px-1.5 py-0.5 text-xs font-mono border uppercase ${STATUS_STYLES[campaign.status] ?? ""}`}
          >
            {campaign.status}
          </span>
        </div>
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
          <Fact label="Opened">{campaign.opened_on}</Fact>
          <Fact label="Closed">{campaign.closed_on}</Fact>
          <Fact label="Cost basis">
            {campaign.cost_basis != null ? `$${Number(campaign.cost_basis).toFixed(3)}` : null}
          </Fact>
          <Fact label="Realized P/L">
            {pnl !== null ? (
              <span className={pnl >= 0 ? "text-gain" : "text-loss"}>
                {pnl >= 0 ? "+" : ""}
                {pnl.toLocaleString("en-US", { style: "currency", currency: "USD" })}
              </span>
            ) : null}
          </Fact>
          <Fact label="Account">{campaign.account}</Fact>
          <Fact label="Strategy">{campaign.strategy}</Fact>
          {Array.isArray(campaign.frontmatter?.thesis_tags) && (
            <div className="col-span-2">
              <div className="text-xs uppercase tracking-wide text-mid-brown">Tags</div>
              <div className="flex gap-1 flex-wrap mt-0.5">
                {campaign.frontmatter.thesis_tags.map((tag) => (
                  <span key={tag} className="px-1 py-0.5 text-xs font-mono border border-ink/40">
                    {tag}
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>
        {campaign.parse_error && (
          <p className="text-xs text-warning font-mono">
            ⚠ frontmatter did not fully parse ({campaign.parse_error}) — showing raw file.
          </p>
        )}
      </div>

      <div className="text-sm text-ink font-serif prose prose-sm max-w-none prose-headings:text-dark-brown prose-p:my-2 prose-ul:my-2 prose-ol:my-2 prose-li:my-0 prose-table:text-xs">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{campaign.body}</ReactMarkdown>
      </div>

      <p className="text-xs text-mid-brown font-mono">
        Synced from {campaign.vault_path} · {new Date(campaign.synced_at).toLocaleString()} ·
        edit in Obsidian, not here — the vault is authoritative.
      </p>
    </div>
  );
}
