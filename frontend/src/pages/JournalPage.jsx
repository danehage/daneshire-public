import { useState, useDeferredValue } from "react";
import { Link } from "react-router-dom";
import ReactMarkdown from "react-markdown";
import {
  useAllJournalEntries,
  useCreateStandaloneJournalEntry,
  useDeleteJournalEntry,
} from "../hooks/useJournal";
import { useShareMode, maskDollarsInText } from "../hooks/useShareMode";

const ENTRY_TYPES = [
  { value: "thesis", label: "Thesis" },
  { value: "note", label: "Note" },
  { value: "entry", label: "Entry" },
  { value: "exit", label: "Exit" },
  { value: "adjustment", label: "Adjustment" },
  { value: "review", label: "Review" },
];

const TYPE_COLORS = {
  thesis: "bg-accent text-warm-white",
  note: "bg-warm-white text-dark-brown",
  entry: "bg-success text-warm-white",
  exit: "bg-error text-warm-white",
  adjustment: "bg-warning text-warm-white",
  review: "bg-light-brown text-warm-white",
  decision: "bg-dark-brown text-warm-white",
};

// The feed filter also offers decision-log rows (#119); they're recorded
// from the scanner or a ticker page, not from the new-entry form.
const FILTER_TYPES = [...ENTRY_TYPES, { value: "decision", label: "Decision" }];

function AddStandaloneEntryForm({ onClose }) {
  const [ticker, setTicker] = useState("");
  const [entryType, setEntryType] = useState("note");
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const createEntry = useCreateStandaloneJournalEntry();

  const handleSubmit = (e) => {
    e.preventDefault();
    if (!ticker.trim() || !content.trim()) return;

    createEntry.mutate(
      {
        ticker: ticker.trim().toUpperCase(),
        entry_type: entryType,
        title: title.trim() || null,
        content: content.trim(),
      },
      {
        onSuccess: () => {
          setTicker("");
          setTitle("");
          setContent("");
          onClose();
        },
      }
    );
  };

  return (
    <form onSubmit={handleSubmit} className="p-4 bg-warm-white border-2 border-ink space-y-3">
      <div className="flex gap-2">
        <input
          type="text"
          value={ticker}
          onChange={(e) => setTicker(e.target.value.toUpperCase())}
          placeholder="TICKER"
          className="w-24 px-3 py-2 border-2 border-ink bg-cream text-sm font-mono uppercase"
          maxLength={10}
          required
        />
        <select
          value={entryType}
          onChange={(e) => setEntryType(e.target.value)}
          className="px-3 py-2 border-2 border-ink bg-cream text-sm"
        >
          {ENTRY_TYPES.map((type) => (
            <option key={type.value} value={type.value}>
              {type.label}
            </option>
          ))}
        </select>
        <input
          type="text"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder="Title (optional)"
          className="flex-1 px-3 py-2 border-2 border-ink bg-cream text-sm"
        />
      </div>
      <textarea
        value={content}
        onChange={(e) => setContent(e.target.value)}
        placeholder="Write your notes here... (supports Markdown)"
        rows={8}
        className="w-full px-3 py-2 border-2 border-ink bg-cream text-sm font-serif resize-y"
        required
      />
      <div className="flex gap-2">
        <button
          type="submit"
          disabled={createEntry.isPending || !ticker.trim() || !content.trim()}
          className="px-4 py-2 bg-accent text-warm-white text-sm font-medium uppercase tracking-wide border-2 border-ink shadow-hard-sm hover:bg-accent-hover disabled:opacity-50"
        >
          {createEntry.isPending ? "Saving..." : "Save Entry"}
        </button>
        <button
          type="button"
          onClick={onClose}
          className="px-3 py-2 text-mid-brown hover:text-ink text-sm"
        >
          Cancel
        </button>
      </div>
    </form>
  );
}

function formatDate(dateString, dateOnly = false) {
  const date = new Date(dateString);
  if (dateOnly) {
    // Vault entry dates are day-precision (stored as midnight UTC) — render
    // in UTC so the local-timezone shift doesn't roll them back a day.
    return date.toLocaleDateString("en-US", {
      month: "short",
      day: "numeric",
      year: "numeric",
      timeZone: "UTC",
    });
  }
  return date.toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

export default function JournalPage() {
  const [showForm, setShowForm] = useState(false);
  const [tickerFilter, setTickerFilter] = useState("");
  const [typeFilter, setTypeFilter] = useState("");

  // Debounce ticker filter to avoid excessive API calls
  const deferredTickerFilter = useDeferredValue(tickerFilter);

  const { data: entries, isLoading } = useAllJournalEntries(
    deferredTickerFilter || null,
    typeFilter || null
  );

  // For delete, we pass null since these may be standalone entries
  const deleteEntry = useDeleteJournalEntry(null);
  const { shareMode } = useShareMode();

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-serif font-bold text-ink">Journal</h1>
        {!showForm && (
          <button
            onClick={() => setShowForm(true)}
            className="px-4 py-2 bg-accent text-warm-white text-sm font-medium uppercase tracking-wide border-2 border-ink shadow-hard-sm hover:bg-accent-hover"
          >
            + New Entry
          </button>
        )}
      </div>

      {showForm && (
        <div className="mb-6">
          <AddStandaloneEntryForm onClose={() => setShowForm(false)} />
        </div>
      )}

      <div className="flex gap-3 mb-4">
        <input
          type="text"
          value={tickerFilter}
          onChange={(e) => setTickerFilter(e.target.value.toUpperCase())}
          placeholder="Filter by ticker..."
          className="px-3 py-2 border-2 border-ink bg-warm-white text-sm w-32 font-mono uppercase"
          maxLength={10}
        />
        <select
          value={typeFilter}
          onChange={(e) => setTypeFilter(e.target.value)}
          className="px-3 py-2 border-2 border-ink bg-warm-white text-sm"
        >
          <option value="">All Types</option>
          {FILTER_TYPES.map((type) => (
            <option key={type.value} value={type.value}>
              {type.label}
            </option>
          ))}
        </select>
      </div>

      {isLoading ? (
        <div className="text-mid-brown">Loading journal entries...</div>
      ) : entries && entries.length > 0 ? (
        <div className="space-y-4">
          {entries.map((entry) => (
            <div
              key={entry.id}
              className="py-4 px-5 bg-warm-white border-2 border-ink"
            >
              <div className="flex items-center justify-between mb-3">
                <div className="flex items-center gap-3 flex-wrap">
                  {entry.ticker && (
                    <span className="text-sm font-mono font-bold text-accent">
                      {entry.ticker}
                    </span>
                  )}
                  <span
                    className={`text-xs px-2 py-0.5 border border-ink uppercase tracking-wide ${
                      TYPE_COLORS[entry.entry_type]
                    }`}
                  >
                    {entry.entry_type}
                  </span>
                  {entry.title && (
                    <span className="text-sm font-medium text-dark-brown">
                      {entry.title}
                    </span>
                  )}
                  {entry.source === "vault" && (
                    <Link
                      to={`/trades/${entry.campaign_id}`}
                      className="text-xs px-2 py-0.5 border border-ink bg-cream text-mid-brown uppercase tracking-wide hover:text-ink hover:bg-warm-white"
                      title="Synced from the DaneBrain vault — read-only. Click to open the campaign."
                    >
                      Vault
                    </Link>
                  )}
                  {entry.source === "decision" && (
                    <Link
                      to="/decisions"
                      className="text-xs px-2 py-0.5 border border-ink bg-cream text-mid-brown uppercase tracking-wide hover:text-ink hover:bg-warm-white"
                      title="From the decision log: read-only. Click for results."
                    >
                      Decision log
                    </Link>
                  )}
                  <span className="text-xs text-light-brown">
                    {formatDate(entry.created_at, entry.source === "vault")}
                  </span>
                </div>
                {entry.source === "app" && (
                  <button
                    onClick={() => deleteEntry.mutate(entry.id)}
                    disabled={deleteEntry.isPending}
                    className="text-light-brown hover:text-error font-bold text-xl"
                  >
                    ×
                  </button>
                )}
              </div>
              <div className="text-sm text-ink font-serif prose prose-sm max-w-none prose-headings:text-dark-brown prose-p:my-2 prose-ul:my-2 prose-ol:my-2 prose-li:my-0">
                <ReactMarkdown>
                  {shareMode ? maskDollarsInText(entry.content) : entry.content}
                </ReactMarkdown>
              </div>
            </div>
          ))}
        </div>
      ) : (
        <div className="text-mid-brown italic">
          No journal entries found.{" "}
          {!showForm && (
            <button
              onClick={() => setShowForm(true)}
              className="text-accent hover:underline"
            >
              Create one?
            </button>
          )}
        </div>
      )}
    </div>
  );
}
