const API_BASE = import.meta.env.VITE_API_URL || '';

export async function getTickerContext(symbol) {
  const res = await fetch(`${API_BASE}/api/ticker/${symbol}/context`);
  if (!res.ok) throw new Error(`${res.status}: ${res.statusText}`);
  return res.json();
}

export async function getTickerNarrative(symbol) {
  const res = await fetch(`${API_BASE}/api/ticker/${symbol}/context/narrative`);
  if (!res.ok) throw new Error(`${res.status}: ${res.statusText}`);
  return res.json();
}
