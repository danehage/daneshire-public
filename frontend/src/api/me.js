const API_BASE = import.meta.env.VITE_API_URL || "";

export async function getMe() {
  const res = await fetch(`${API_BASE}/api/me`);
  if (!res.ok) throw new Error(`${res.status}: ${res.statusText}`);
  return res.json();
}
