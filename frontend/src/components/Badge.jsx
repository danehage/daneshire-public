/**
 * Badge — the small uppercase status/category pill. Centralizes the tone → color
 * mapping so every badge in the app draws from one palette.
 *
 * Tones are deliberately *named by appearance* (accent, success, …) not by
 * meaning, because two unrelated vocabularies use them — watchlist `status`
 * (see STATUS_TONES) and journal `entry_type` (see ENTRY_TYPE_TONES). Today they
 * overlap (e.g. `accent` = both "owned" and "thesis"); collapsing that collision
 * is a palette decision — change the maps below in one place when ready.
 */
const TONES = {
  neutral: "bg-warm-white text-dark-brown border-ink",
  accent: "bg-accent text-warm-white border-ink",
  success: "bg-success text-warm-white border-ink",
  muted: "bg-light-brown text-warm-white border-ink",
  error: "bg-error text-warm-white border-ink",
  warning: "bg-warning text-warm-white border-ink",
  "mid-brown": "bg-mid-brown text-warm-white border-ink",
};

// Watchlist item status → tone.
export const STATUS_TONES = {
  watching: "neutral",
  owned: "accent",
  position_open: "success",
  closed: "muted",
};

// Journal entry type → tone.
export const ENTRY_TYPE_TONES = {
  thesis: "accent",
  note: "muted",
  entry: "success",
  exit: "error",
  adjustment: "warning",
  review: "mid-brown",
};

export default function Badge({
  tone = "neutral",
  border = 1,
  as: Tag = "span",
  className = "",
  children,
  ...props
}) {
  const toneClass = TONES[tone] ?? TONES.neutral;
  const borderClass = border === 2 ? "border-2" : "border";
  return (
    <Tag
      className={`inline-block text-xs px-2 py-0.5 ${borderClass} uppercase tracking-wide font-medium ${toneClass} ${className}`}
      {...props}
    >
      {children}
    </Tag>
  );
}
