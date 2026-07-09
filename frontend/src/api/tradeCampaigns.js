const API_BASE = import.meta.env.VITE_API_URL || "";

export async function getTradeCampaigns({ ticker, includeArchived } = {}) {
  const params = new URLSearchParams();
  if (ticker) params.set("ticker", ticker);
  if (includeArchived) params.set("include_archived", "true");
  const qs = params.toString();
  const res = await fetch(`${API_BASE}/api/trade-campaigns${qs ? `?${qs}` : ""}`);
  if (!res.ok) throw new Error(`${res.status}: ${res.statusText}`);
  return res.json();
}

export async function getTradeCampaign(campaignId) {
  const res = await fetch(`${API_BASE}/api/trade-campaigns/${campaignId}`);
  if (!res.ok) throw new Error(`${res.status}: ${res.statusText}`);
  return res.json();
}
