import { useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { useRole } from "../hooks/useMe";
import { useShareMode } from "../hooks/useShareMode";

const navItems = [
  { path: "/", label: "Dashboard", ownerOnly: true },
  { path: "/watchlist", label: "Watchlist", ownerOnly: true },
  { path: "/portfolio", label: "Portfolio", ownerOnly: true },
  { path: "/trades", label: "Trades", ownerOnly: true },
  { path: "/journal", label: "Journal", ownerOnly: true },
  { path: "/decisions", label: "Decisions", ownerOnly: true },
  { path: "/scanner", label: "Scanner" },
  { path: "/earnings", label: "Earnings" },
  { path: "/alerts", label: "Alerts", ownerOnly: true },
];

function TickerSearch() {
  const [ticker, setTicker] = useState("");
  const navigate = useNavigate();

  const handleSubmit = (e) => {
    e.preventDefault();
    if (ticker.trim()) {
      navigate(`/ticker/${ticker.trim().toUpperCase()}`);
      setTicker("");
    }
  };

  return (
    <form onSubmit={handleSubmit} className="flex shrink-0">
      <input
        type="text"
        value={ticker}
        onChange={(e) => setTicker(e.target.value.toUpperCase())}
        placeholder="AAPL"
        className="w-16 sm:w-20 px-2 py-1 text-sm border-2 border-ink bg-warm-white font-mono uppercase"
        maxLength={5}
      />
      <button
        type="submit"
        className="px-2 py-1 text-sm bg-ink text-warm-white border-2 border-ink hover:bg-dark-brown"
      >
        Go
      </button>
    </form>
  );
}

function ShareModeToggle() {
  const { shareMode, toggle } = useShareMode();
  return (
    <button
      type="button"
      onClick={toggle}
      title="Masks your balances, P/L dollars, and share counts. Market data stays visible."
      className={`flex items-center gap-2 px-3 py-1.5 text-xs font-medium uppercase tracking-wide border-2 transition-colors ${
        shareMode
          ? "bg-ink text-warm-white border-ink"
          : "bg-warm-white text-mid-brown border-light-brown hover:border-ink hover:text-ink"
      }`}
    >
      <span
        className={`inline-block w-2 h-2 rounded-full ${
          shareMode ? "bg-warning" : "bg-light-brown"
        }`}
      />
      Share mode {shareMode ? "on" : "off"}
    </button>
  );
}

export default function Layout({ children }) {
  const location = useLocation();
  const role = useRole();
  // Until the role is known, show only the guest-safe items so a guest
  // never sees owner-only links flash in.
  const visibleItems = navItems.filter(
    (item) => !item.ownerOnly || role === "owner"
  );

  return (
    <div className="min-h-screen bg-cream">
      <nav className="bg-warm-white border-b-2 border-ink">
        <div className="max-w-6xl mx-auto px-4 sm:px-6">
          {/* Masthead row: logo left, ticker search right */}
          <div className="flex items-center justify-between gap-4 h-14">
            <Link
              to={role === "guest" ? "/scanner" : "/"}
              className="text-lg sm:text-xl font-serif font-bold tracking-tight shrink-0"
            >
              <span className="text-accent">Dane</span>
              <span className="text-ink">shire Hathaway</span>
            </Link>
            <TickerSearch />
          </div>
          {/* Nav row: full container width, scrolls only on narrow screens */}
          <div className="flex overflow-x-auto border-t border-light-brown">
            {visibleItems.map((item) => (
              <Link
                key={item.path}
                to={item.path}
                className={`px-3 sm:px-4 py-2 text-xs sm:text-sm font-medium uppercase tracking-wide border-2 transition-all whitespace-nowrap ${
                  location.pathname === item.path
                    ? "bg-warm-white border-ink text-ink"
                    : "border-transparent text-mid-brown hover:border-ink hover:text-ink"
                }`}
              >
                {item.label}
              </Link>
            ))}
          </div>
        </div>
      </nav>
      <main className="max-w-6xl mx-auto px-4 sm:px-6 py-6 sm:py-8">{children}</main>
      {role === "owner" && (
        <footer className="max-w-6xl mx-auto px-4 sm:px-6 pb-6 flex justify-end">
          <ShareModeToggle />
        </footer>
      )}
    </div>
  );
}
