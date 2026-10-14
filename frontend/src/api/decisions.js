const API_BASE = import.meta.env.VITE_API_URL || "";

// The decision API's errors carry a useful detail (a 502 names the ticker
// whose quote failed; a 409 explains the delete window), so surface it.
async function fail(res) {
  let detail = res.statusText;
  try {
    const body = await res.json();
    if (typeof body?.detail === "string") detail = body.detail;
  } catch {
    // non-JSON error body: keep statusText
  }
  throw new Error(`${res.status}: ${detail}`);
}

export async function getDecisions({ ticker, decision, limit } = {}) {
  const params = new URLSearchParams();
  if (ticker) params.append("ticker", ticker);
  if (decision) params.append("decision", decision);
  if (limit) params.append("limit", String(limit));
  const qs = params.toString();
  const res = await fetch(`${API_BASE}/api/decisions${qs ? `?${qs}` : ""}`);
  if (!res.ok) await fail(res);
  return res.json();
}

export async function getDecisionSummary() {
  const res = await fetch(`${API_BASE}/api/decisions/summary`);
  if (!res.ok) await fail(res);
  return res.json();
}

export async function createDecision(data) {
  const res = await fetch(`${API_BASE}/api/decisions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  if (!res.ok) await fail(res);
  return res.json();
}

export async function deleteDecision(decisionId) {
  const res = await fetch(`${API_BASE}/api/decisions/${decisionId}`, {
    method: "DELETE",
  });
  if (!res.ok) await fail(res);
}
