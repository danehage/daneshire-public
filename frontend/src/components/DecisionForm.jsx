import { useState } from "react";
import { useCreateDecision } from "../hooks/useDecisions";

const MAX_LEN = 280;

/**
 * Modal for recording a buy/pass call (#119). The price is not an input:
 * the server takes the ticker and SPY quotes at submit, and the decision
 * is frozen from then on (delete within 15 minutes, otherwise supersede).
 *
 * Props: ticker, source ("scanner" | "ticker" | "manual"), scanId (optional),
 * supersedes (optional decision being replaced), onClose.
 */
export default function DecisionForm({ ticker, source = "manual", scanId = null, supersedes = null, onClose }) {
  const [call, setCall] = useState(supersedes?.decision ?? null);
  const [conviction, setConviction] = useState(null);
  const [thesis, setThesis] = useState("");
  const [invalidation, setInvalidation] = useState("");
  const createDecision = useCreateDecision();

  const ready = call && conviction && thesis.trim() && invalidation.trim();

  const handleSubmit = (e) => {
    e.preventDefault();
    if (!ready) return;
    createDecision.mutate(
      {
        ticker,
        decision: call,
        conviction,
        thesis: thesis.trim(),
        invalidation: invalidation.trim(),
        source,
        ...(scanId ? { scan_id: scanId } : {}),
        ...(supersedes ? { supersedes_id: supersedes.id } : {}),
      },
      { onSuccess: () => setTimeout(onClose, 1200) }
    );
  };

  const saved = createDecision.data;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="fixed inset-0 bg-ink/40" />
      <div className="relative z-10 bg-warm-white border-4 border-ink shadow-hard w-full max-w-lg max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between px-6 py-4 border-b-2 border-ink">
          <h2 className="font-serif font-bold text-xl">
            {supersedes ? "Change decision" : "Record decision"}:{" "}
            <span className="font-mono">{ticker}</span>
          </h2>
          <button
            type="button"
            onClick={onClose}
            className="font-mono text-lg leading-none px-2 py-1 hover:bg-cream border border-ink"
            aria-label="Close"
          >
            ×
          </button>
        </div>

        {saved ? (
          <div className="p-6 space-y-2">
            <p className="text-ink">
              Recorded <strong>{saved.decision.toUpperCase()}</strong> at{" "}
              <span className="font-mono">${Number(saved.price_at_decision).toFixed(2)}</span>{" "}
              (SPY <span className="font-mono">${Number(saved.spy_at_decision).toFixed(2)}</span>).
            </p>
            <p className="text-sm text-mid-brown">
              Scored against SPY at 21, 63 and 126 sessions.
            </p>
          </div>
        ) : (
          <form onSubmit={handleSubmit} className="p-6 space-y-5">
            {supersedes && (
              <p className="text-sm text-mid-brown border-2 border-light-brown bg-cream p-3">
                Replaces your {supersedes.decision.toUpperCase()} (conviction{" "}
                {supersedes.conviction}) from{" "}
                {new Date(supersedes.created_at).toLocaleDateString("en-US", {
                  month: "short",
                  day: "numeric",
                  year: "numeric",
                })}
                . Both calls stay in the record and both count.
              </p>
            )}

            <div>
              <div className="text-xs font-medium uppercase tracking-wide text-dark-brown mb-2">
                Call
              </div>
              <div className="flex gap-2">
                {["buy", "pass"].map((value) => (
                  <button
                    key={value}
                    type="button"
                    onClick={() => setCall(value)}
                    aria-pressed={call === value}
                    className={`flex-1 px-4 py-2 text-sm font-medium uppercase tracking-wide border-2 border-ink ${
                      call === value
                        ? value === "buy"
                          ? "bg-success text-warm-white"
                          : "bg-mid-brown text-warm-white"
                        : "bg-cream text-ink hover:bg-warm-white"
                    }`}
                  >
                    {value}
                  </button>
                ))}
              </div>
            </div>

            <div>
              <div className="text-xs font-medium uppercase tracking-wide text-dark-brown mb-2">
                Conviction
              </div>
              <div className="flex gap-2">
                {[1, 2, 3, 4, 5].map((n) => (
                  <button
                    key={n}
                    type="button"
                    onClick={() => setConviction(n)}
                    aria-pressed={conviction === n}
                    className={`w-10 h-10 font-mono border-2 border-ink ${
                      conviction === n
                        ? "bg-accent text-warm-white"
                        : "bg-cream text-ink hover:bg-warm-white"
                    }`}
                  >
                    {n}
                  </button>
                ))}
              </div>
            </div>

            <TextField
              label="Thesis (one line)"
              value={thesis}
              onChange={setThesis}
              placeholder="Why this call, in a sentence"
            />
            <TextField
              label="What would prove it wrong"
              value={invalidation}
              onChange={setInvalidation}
              placeholder="The price or event that kills the thesis"
            />

            <p className="text-xs text-mid-brown">
              The price is locked when you save: the server takes the {ticker} and
              SPY quotes at that moment. You can delete within 15 minutes; after
              that the call is permanent, and a change of mind is a new decision.
            </p>

            {createDecision.isError && (
              <p className="text-sm text-error border-2 border-error bg-error/10 p-2">
                {createDecision.error.message}
              </p>
            )}

            <div className="flex gap-2">
              <button
                type="submit"
                disabled={!ready || createDecision.isPending}
                className="px-4 py-2 bg-accent text-warm-white text-sm font-medium uppercase tracking-wide border-2 border-ink shadow-hard-sm hover:bg-accent-hover disabled:opacity-50"
              >
                {createDecision.isPending ? "Saving..." : "Record decision"}
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
        )}
      </div>
    </div>
  );
}

function TextField({ label, value, onChange, placeholder }) {
  return (
    <label className="block">
      <span className="flex justify-between text-xs font-medium uppercase tracking-wide text-dark-brown mb-2">
        {label}
        <span className={`font-mono normal-case ${value.length > MAX_LEN - 20 ? "text-warning" : "text-light-brown"}`}>
          {value.length}/{MAX_LEN}
        </span>
      </span>
      <textarea
        value={value}
        onChange={(e) => onChange(e.target.value)}
        maxLength={MAX_LEN}
        rows={2}
        placeholder={placeholder}
        className="w-full px-3 py-2 border-2 border-ink bg-cream text-sm font-serif resize-y"
        required
      />
    </label>
  );
}
