import { useState } from "react";
import { useUniverses, useScan } from "../hooks/useScanner";
import { useCreateWatchlistItem } from "../hooks/useWatchlist";
import { useRole } from "../hooks/useMe";

function UniverseSelector({ value, onChange, universes }) {
  return (
    <div className="space-y-2">
      <label className="block text-xs font-medium uppercase tracking-wide text-dark-brown">
        Universe
      </label>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="w-full px-4 py-3 border-2 border-ink bg-warm-white text-ink font-serif"
      >
        {universes?.map((u) => (
          <option key={u.name} value={u.name}>
            {`${u.name.toUpperCase()} (${
              u.size == null ? "size set at scan" : `${u.size} stocks`
            }) — ${u.description}`}
          </option>
        ))}
      </select>
      {value === "combined" && (
        <p className="text-xs text-mid-brown">
          The quality gate&apos;s backtest evidence covers the S&amp;P 500
          only; across the wider universe it is a filter, not a tested edge.
        </p>
      )}
    </div>
  );
}

function ProgressBar({ current, total, found, errors }) {
  const percent = total > 0 ? Math.round((current / total) * 100) : 0;

  return (
    <div className="space-y-2">
      <div className="flex justify-between text-sm text-mid-brown">
        <span>
          Analyzing {current} of {total}
          {errors > 0 && (
            <span className="text-warning ml-2">({errors} errors)</span>
          )}
        </span>
        <span>{found} opportunities found</span>
      </div>
      <div className="h-4 border-2 border-ink bg-cream">
        <div
          className="h-full bg-accent transition-all duration-300"
          style={{ width: `${percent}%` }}
        />
      </div>
    </div>
  );
}

function SignalBadge({ signal }) {
  // Color code based on signal content
  let colorClass = "bg-warm-white text-dark-brown border-light-brown";

  if (
    signal.includes("rising") ||
    signal.includes("shrinking") ||
    signal.includes("Higher") ||
    signal.includes("Held") ||
    signal.includes("draining") ||
    signal.includes("closes above")
  ) {
    colorClass = "bg-success text-warm-white border-ink";
  } else if (
    signal.includes("falling") ||
    signal.includes("slipping") ||
    signal.includes("deepening") ||
    signal.includes("fading")
  ) {
    colorClass = "bg-error text-warm-white border-ink";
  } else if (signal.includes("support") || signal.includes("Uptrend")) {
    colorClass = "bg-accent text-warm-white border-ink";
  } else if (signal.includes("Recovery candidate")) {
    colorClass = "bg-warning text-warm-white border-ink";
  } else if (signal.includes("cap") || signal.includes("liquid")) {
    colorClass = "bg-light-brown text-warm-white border-ink";
  }

  return (
    <span
      className={`inline-block text-xs px-2 py-0.5 border ${colorClass} mr-1 mb-1`}
    >
      {signal}
    </span>
  );
}

function SetupBadge({ setup }) {
  if (!setup) return <span className="text-light-brown">—</span>;

  const colorClass =
    setup === "Knife"
      ? "bg-error text-warm-white"
      : setup === "Fading bounce"
      ? "bg-warning text-warm-white"
      : setup === "Uptrend pullback"
      ? "bg-accent text-warm-white"
      : "bg-success text-warm-white"; // Just bottomed / Basing

  return (
    <span
      className={`inline-block px-2 py-0.5 text-xs font-medium border-2 border-ink whitespace-nowrap ${colorClass}`}
    >
      {setup}
      {setup === "Fading bounce" && " ⚠"}
    </span>
  );
}

function GateFailures({ fails }) {
  return (
    <div className="flex flex-wrap gap-1 mt-1">
      {fails.map((reason) => (
        <span
          key={reason}
          className="inline-block text-xs px-1.5 py-0.5 border border-ink bg-error text-warm-white whitespace-nowrap"
        >
          {reason}
        </span>
      ))}
    </div>
  );
}

// Fact columns (#117). The scanner is a research shortlist, not a ranker:
// each column is a fact Dane can sort on. The legacy composite score stays
// as the last column for one quarter (remove ~2027-01-10); it is never the
// default sort.
function signedPct(v, digits = 1) {
  if (v == null) return null;
  return `${v > 0 ? "+" : ""}${v.toFixed(digits)}%`;
}

function trendRank(r) {
  if (r.dist_50 == null && r.dist_200 == null) return null;
  return (r.dist_50 > 0 ? 1 : 0) + (r.dist_200 > 0 ? 1 : 0);
}

function formatCap(v) {
  if (!v) return "—";
  if (v >= 1e12) return `$${(v / 1e12).toFixed(2)}T`;
  if (v >= 1e9) return `$${(v / 1e9).toFixed(1)}B`;
  return `$${(v / 1e6).toFixed(0)}M`;
}

const COLUMNS = [
  { key: "ticker", label: "Ticker", dir: "asc", get: (r) => r.ticker },
  { key: "price", label: "Price", dir: "desc", get: (r) => r.price },
  {
    key: "pct_off_high",
    label: "Off 52w High",
    dir: "asc",
    get: (r) => r.pct_off_high,
    title: "% below the 52-week high",
  },
  {
    key: "earnings_reaction_vs_spy",
    label: "Earnings Rxn vs SPY",
    dir: "desc",
    get: (r) => r.earnings_reaction_vs_spy,
    title:
      "Last report: close before the report date to close of the next session, minus SPY over the same days",
  },
  {
    key: "eps_surprise_pct",
    label: "Last EPS",
    dir: "desc",
    get: (r) => r.eps_surprise_pct,
    title: "Last reported EPS vs the consensus estimate",
  },
  {
    key: "trend",
    label: "Trend",
    dir: "desc",
    get: trendRank,
    title: "Price above (▲) or below (▼) its 50- and 200-day moving averages",
  },
  { key: "hv_rank", label: "HV Rank", dir: "desc", get: (r) => r.hv_rank },
  {
    key: "days_to_earnings",
    label: "Next Report",
    dir: "asc",
    get: (r) => r.days_to_earnings,
  },
  {
    key: "sector",
    label: "Sector",
    dir: "asc",
    get: (r) => (r.sector ? r.sector.toLowerCase() : null),
  },
  { key: "market_cap", label: "Mkt Cap", dir: "desc", get: (r) => r.market_cap || null },
  {
    key: "score",
    label: "Legacy Score",
    dir: "desc",
    get: (r) => r.score,
    title:
      "Old composite score. Its top picks trailed SPY out-of-sample (#114); shown until ~2027-01-10, then removed",
  },
];

const DEFAULT_SORT = { key: "ticker", dir: "asc" };

function sortRows(rows, { key, dir }) {
  const col = COLUMNS.find((c) => c.key === key) ?? COLUMNS[0];
  const sign = dir === "asc" ? 1 : -1;
  return [...rows].sort((a, b) => {
    const va = col.get(a);
    const vb = col.get(b);
    // Missing values sort last in either direction.
    if (va == null && vb == null) return a.ticker.localeCompare(b.ticker);
    if (va == null) return 1;
    if (vb == null) return -1;
    if (va < vb) return -sign;
    if (va > vb) return sign;
    return a.ticker.localeCompare(b.ticker);
  });
}

function SortHeader({ column, sort, onSort }) {
  const active = sort.key === column.key;
  const arrow = active ? (sort.dir === "asc" ? " ↑" : " ↓") : "";
  return (
    <th
      className="text-left py-3 px-3 whitespace-nowrap"
      aria-sort={
        active ? (sort.dir === "asc" ? "ascending" : "descending") : "none"
      }
    >
      <button
        type="button"
        onClick={() => onSort(column)}
        title={column.title}
        className={`text-xs font-medium uppercase tracking-wide hover:text-ink ${
          active ? "text-ink" : "text-dark-brown"
        } ${column.key === "score" ? "italic" : ""}`}
      >
        {column.label}
        {arrow}
      </button>
    </th>
  );
}

function Pct({ value, title }) {
  if (value == null) return <span className="text-light-brown">—</span>;
  const color = value > 0 ? "text-gain" : value < 0 ? "text-loss" : "text-ink";
  return (
    <span className={`font-mono ${color}`} title={title}>
      {signedPct(value)}
    </span>
  );
}

function EpsCell({ result }) {
  const s = result.eps_surprise_pct;
  const title =
    result.last_eps_actual != null
      ? `${result.last_earnings_date ?? ""}: actual ${result.last_eps_actual}` +
        (result.last_eps_estimate != null
          ? ` vs est. ${result.last_eps_estimate}`
          : "")
      : undefined;
  if (s == null) {
    return (
      <span className="text-light-brown" title={title}>
        —
      </span>
    );
  }
  const label = s > 0 ? "Beat" : s < 0 ? "Miss" : "In line";
  const color = s > 0 ? "text-gain" : s < 0 ? "text-loss" : "text-ink";
  return (
    <span className={`whitespace-nowrap ${color}`} title={title}>
      {label}
      {s !== 0 && <span className="font-mono text-xs ml-1">{signedPct(s)}</span>}
    </span>
  );
}

function MaMark({ label, dist }) {
  if (dist == null) return <span className="text-light-brown">{label} —</span>;
  const above = dist > 0;
  return (
    <span
      className={above ? "text-gain" : "text-loss"}
      title={`${signedPct(dist)} vs ${label}-day MA`}
    >
      {above ? "▲" : "▼"}
      {label}
    </span>
  );
}

function ResultRow({ result, onAddToWatchlist, isAdding }) {
  const [expanded, setExpanded] = useState(false);
  const failedGate = result.quality_pass === false;

  return (
    <>
      <tr
        className={`border-b-2 border-ink hover:bg-warm-white transition-colors cursor-pointer ${
          expanded ? "bg-warm-white" : ""
        } ${failedGate ? "opacity-60" : ""}`}
        onClick={() => setExpanded(!expanded)}
      >
        <td className="py-3 px-3">
          <div className="flex items-center gap-2">
            <span
              className={`text-xs transition-transform ${
                expanded ? "rotate-90" : ""
              }`}
            >
              ▶
            </span>
            <span className="font-serif text-lg text-ink">{result.ticker}</span>
          </div>
          {failedGate && <GateFailures fails={result.quality_fails} />}
        </td>
        <td className="py-3 px-3 font-mono">${result.price.toFixed(2)}</td>
        <td className="py-3 px-3 text-sm">
          <Pct value={result.pct_off_high} />
        </td>
        <td className="py-3 px-3 text-sm">
          <Pct
            value={result.earnings_reaction_vs_spy}
            title={
              result.earnings_reaction_pct != null
                ? `${result.last_earnings_date}: ${signedPct(
                    result.earnings_reaction_pct
                  )} raw`
                : undefined
            }
          />
        </td>
        <td className="py-3 px-3 text-sm">
          <EpsCell result={result} />
        </td>
        <td className="py-3 px-3 text-sm font-mono whitespace-nowrap space-x-2">
          <MaMark label="50" dist={result.dist_50} />
          <MaMark label="200" dist={result.dist_200} />
        </td>
        <td className="py-3 px-3 text-sm font-mono">
          {result.hv_rank != null ? result.hv_rank.toFixed(0) : "—"}
        </td>
        <td
          className="py-3 px-3 text-sm font-mono whitespace-nowrap"
          title={result.earnings_date ?? undefined}
        >
          {result.days_to_earnings != null ? `${result.days_to_earnings}d` : "—"}
        </td>
        <td className="py-3 px-3 text-sm text-mid-brown whitespace-nowrap">
          {result.sector ?? "—"}
        </td>
        <td className="py-3 px-3 text-sm font-mono whitespace-nowrap">
          {formatCap(result.market_cap)}
        </td>
        <td className="py-3 px-3 text-sm font-mono text-mid-brown">
          {result.score}
        </td>
        <td className="py-3 px-3" onClick={(e) => e.stopPropagation()}>
          {onAddToWatchlist && (
            <button
              onClick={() => onAddToWatchlist(result)}
              disabled={isAdding}
              className="px-3 py-1 text-xs font-medium uppercase tracking-wide border-2 border-ink bg-accent text-warm-white hover:bg-accent-hover shadow-hard-sm disabled:opacity-50"
            >
              {isAdding ? "..." : "+ Add"}
            </button>
          )}
        </td>
      </tr>
      {expanded && (
        <tr className="bg-cream border-b-2 border-ink">
          <td colSpan={COLUMNS.length + 1} className="px-6 py-4">
            <div className="grid grid-cols-4 gap-6 text-sm">
              <div>
                <h4 className="text-xs font-medium uppercase tracking-wide text-dark-brown mb-2">
                  Price Levels
                </h4>
                <p>
                  <span className="text-mid-brown">Support:</span> $
                  {result.support.toFixed(2)}
                </p>
                <p>
                  <span className="text-mid-brown">Resistance:</span> $
                  {result.resistance.toFixed(2)}
                </p>
                <p>
                  <span className="text-mid-brown">52w Range:</span>{" "}
                  {result.range_position.toFixed(0)}%
                </p>
                <p>
                  <span className="text-mid-brown">RSI:</span>{" "}
                  {result.rsi.toFixed(0)}
                </p>
              </div>
              <div>
                <h4 className="text-xs font-medium uppercase tracking-wide text-dark-brown mb-2">
                  Moving Averages
                </h4>
                <p>
                  <span className="text-mid-brown">Dist 50-MA:</span>{" "}
                  {result.dist_50?.toFixed(1) ?? "—"}%
                </p>
                <p>
                  <span className="text-mid-brown">Dist 200-MA:</span>{" "}
                  {result.dist_200?.toFixed(1) ?? "—"}%
                </p>
                <p>
                  <span className="text-mid-brown">MA Slope:</span>{" "}
                  {result.ma_slope?.toFixed(2) ?? "—"}%
                </p>
              </div>
              <div>
                <h4 className="text-xs font-medium uppercase tracking-wide text-dark-brown mb-2">
                  Volume
                </h4>
                <p>
                  <span className="text-mid-brown">Volume:</span>{" "}
                  {(result.volume / 1000000).toFixed(2)}M
                </p>
                <p>
                  <span className="text-mid-brown">Avg Volume:</span>{" "}
                  {(result.avg_volume / 1000000).toFixed(2)}M
                </p>
                <p>
                  <span className="text-mid-brown">Pace:</span>{" "}
                  {result.volume_pace.toFixed(2)}x
                  {result.volume_pace_reliable && " ✓"}
                </p>
              </div>
              <div>
                <h4 className="text-xs font-medium uppercase tracking-wide text-dark-brown mb-2">
                  Volatility
                </h4>
                <p>
                  <span className="text-mid-brown">HV Rank:</span>{" "}
                  {result.hv_rank?.toFixed(0) ?? "—"}%
                </p>
                <p>
                  <span className="text-mid-brown">Current HV:</span>{" "}
                  {result.current_hv?.toFixed(1) ?? "—"}%
                </p>
                <p>
                  <span className="text-mid-brown">10d Momentum:</span>{" "}
                  {result.momentum_10d.toFixed(1)}%
                </p>
              </div>
            </div>
            <div className="mt-4">
              <h4 className="text-xs font-medium uppercase tracking-wide text-dark-brown mb-2">
                Legacy score detail
                <span className="ml-2 normal-case tracking-normal text-mid-brown">
                  (retiring ~2027-01-10)
                </span>
              </h4>
              <div className="flex flex-wrap items-center gap-2">
                <SetupBadge setup={result.setup} />
                {result.days_since_low != null && (
                  <span className="text-xs text-mid-brown">
                    {result.days_since_low}d since low
                    {result.pct_above_low != null &&
                      `, +${result.pct_above_low.toFixed(0)}% off it`}
                  </span>
                )}
              </div>
              <div className="flex flex-wrap mt-2">
                {result.signals.map((signal, i) => (
                  <SignalBadge key={i} signal={signal} />
                ))}
              </div>
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

function ResultsTable({ results, onAddToWatchlist, addingTicker, sort, onSort }) {
  if (!results || results.length === 0) {
    return (
      <div className="text-center py-12 text-mid-brown">
        No results to display
      </div>
    );
  }

  return (
    <div className="border-2 border-ink shadow-hard bg-warm-white overflow-x-auto">
      <table className="w-full">
        <thead>
          <tr className="border-b-2 border-ink bg-cream">
            {COLUMNS.map((column) => (
              <SortHeader
                key={column.key}
                column={column}
                sort={sort}
                onSort={onSort}
              />
            ))}
            <th className="text-left py-3 px-3 text-xs font-medium uppercase tracking-wide text-dark-brown">
              Action
            </th>
          </tr>
        </thead>
        <tbody>
          {results.map((result) => (
            <ResultRow
              key={result.ticker}
              result={result}
              onAddToWatchlist={onAddToWatchlist}
              isAdding={addingTicker === result.ticker}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function ScannerPage() {
  const [selectedUniverse, setSelectedUniverse] = useState("combined");
  const [useCache, setUseCache] = useState(true);
  const [addingTicker, setAddingTicker] = useState(null);
  const [showFiltered, setShowFiltered] = useState(false);
  const [sort, setSort] = useState(DEFAULT_SORT);

  const { data: universes, isLoading: universesLoading } = useUniverses();
  const {
    startScan,
    isStarting,
    progress,
    isScanning,
    results,
    resultsLoading,
    error,
    reset,
  } = useScan();

  const addToWatchlist = useCreateWatchlistItem();
  const role = useRole();

  // ADR-0007 quality gate (#116): rows that failed are hidden by default.
  // null means the gate didn't run, so those rows stay visible.
  const allRows = results?.results ?? [];
  const filteredOut = allRows.filter((r) => r.quality_pass === false);
  const shownRows = sortRows(
    showFiltered ? allRows : allRows.filter((r) => r.quality_pass !== false),
    sort
  );
  const gateRan = allRows.some((r) => r.quality_pass != null);

  // Re-clicking the active column flips direction; a new column starts in
  // its natural direction (e.g. biggest earnings reaction first).
  const handleSort = (column) =>
    setSort((s) =>
      s.key === column.key
        ? { key: s.key, dir: s.dir === "asc" ? "desc" : "asc" }
        : { key: column.key, dir: column.dir }
    );

  const handleStartScan = () => {
    reset();
    startScan({ universe: selectedUniverse, use_cache: useCache });
  };

  const handleAddToWatchlist = async (result) => {
    setAddingTicker(result.ticker);
    try {
      await addToWatchlist.mutateAsync({
        ticker: result.ticker,
        status: "watching",
        tags: result.signals.slice(0, 3), // First 3 signals as tags
      });
    } finally {
      setAddingTicker(null);
    }
  };

  return (
    <div className="space-y-8">
      {/* Header */}
      <div>
        <h1 className="font-serif text-4xl text-ink mb-2">Scanner</h1>
        <p className="text-mid-brown">
          A quality-gated research shortlist. Sort by any column; no column
          is a buy signal.
        </p>
      </div>

      {/* Controls */}
      <div className="border-2 border-ink shadow-hard bg-warm-white p-6">
        <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
          <UniverseSelector
            value={selectedUniverse}
            onChange={setSelectedUniverse}
            universes={universes}
          />

          <div className="space-y-2">
            <label className="block text-xs font-medium uppercase tracking-wide text-dark-brown">
              Options
            </label>
            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="checkbox"
                checked={useCache}
                onChange={(e) => setUseCache(e.target.checked)}
                className="w-4 h-4 border-2 border-ink"
              />
              <span className="text-sm text-ink">
                Use cached quotes (faster)
              </span>
            </label>
          </div>

          <div className="flex items-end">
            <button
              onClick={handleStartScan}
              disabled={isStarting || isScanning || universesLoading}
              className="w-full px-6 py-3 bg-accent text-warm-white font-medium uppercase tracking-wide border-2 border-ink shadow-hard hover:bg-accent-hover disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              {isStarting
                ? "Starting..."
                : isScanning
                ? "Scanning..."
                : "Run Scan"}
            </button>
          </div>
        </div>
      </div>

      {/* Progress */}
      {isScanning && progress && (
        <div className="border-2 border-ink shadow-hard bg-warm-white p-6">
          <ProgressBar
            current={progress.current}
            total={progress.total}
            found={progress.found}
            errors={progress.errors}
          />
        </div>
      )}

      {/* Error */}
      {error && (
        <div className="border-2 border-error bg-error/10 p-4 text-error">
          {error}
        </div>
      )}

      {/* Results — partial snapshots stream in while the scan runs */}
      {results && allRows.length > 0 && (
        <div className="space-y-4">
          {/* Warning banner if there were errors */}
          {results.warning && (
            <div className="border-2 border-warning bg-warning/10 p-4 text-warning">
              <strong>Warning:</strong> {results.warning}
            </div>
          )}

          <div className="flex items-center justify-between">
            <h2 className="font-serif text-2xl text-ink">
              Results ({allRows.length - filteredOut.length} passed quality gate)
              {results.partial && (
                <span className="ml-3 align-middle inline-block px-2 py-1 text-xs font-medium uppercase tracking-wide bg-warning text-warm-white border-2 border-ink animate-pulse">
                  partial — scan running, rows still arriving
                </span>
              )}
            </h2>
            <span className="text-sm text-mid-brown">
              {results.partial
                ? `${progress?.current ?? results.total_analyzed} of ${progress?.total ?? "?"} analyzed`
                : `Scanned ${results.total_analyzed} stocks`}
              {results.errors > 0 && (
                <span className="text-warning ml-1">
                  ({results.errors} failed)
                </span>
              )}
            </span>
          </div>
          <div className="flex items-center justify-between text-sm">
            {gateRan ? (
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={showFiltered}
                  onChange={(e) => setShowFiltered(e.target.checked)}
                  className="w-4 h-4 border-2 border-ink"
                />
                <span className="text-ink">
                  Show filtered out ({filteredOut.length})
                </span>
              </label>
            ) : (
              <span className="text-warning">
                Quality gate unavailable for this scan; results are unfiltered.
              </span>
            )}
          </div>
          <ResultsTable
            results={shownRows}
            onAddToWatchlist={role === "owner" ? handleAddToWatchlist : null}
            addingTicker={addingTicker}
            sort={sort}
            onSort={handleSort}
          />
        </div>
      )}

      {/* Loading results */}
      {resultsLoading && !results && !isScanning && (
        <div className="text-center py-12 text-mid-brown">
          Loading results...
        </div>
      )}
    </div>
  );
}
