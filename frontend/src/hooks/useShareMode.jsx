import { createContext, useCallback, useContext, useEffect, useState } from "react";

// Share mode masks Dane's own money — balances, market values, P/L dollars,
// and share/contract quantities — so the app can be shown to someone without
// exposing position sizes. Market data (per-share prices, percentages,
// indicators, scanner/earnings output) stays visible.
const ShareModeContext = createContext({ shareMode: false, toggle: () => {} });

const STORAGE_KEY = "shareMode";

export function ShareModeProvider({ children }) {
  const [shareMode, setShareMode] = useState(
    () => localStorage.getItem(STORAGE_KEY) === "on"
  );

  useEffect(() => {
    localStorage.setItem(STORAGE_KEY, shareMode ? "on" : "off");
  }, [shareMode]);

  const toggle = useCallback(() => setShareMode((v) => !v), []);

  return (
    <ShareModeContext.Provider value={{ shareMode, toggle }}>
      {children}
    </ShareModeContext.Provider>
  );
}

export function useShareMode() {
  return useContext(ShareModeContext);
}

export const MASKED_MONEY = "$XX";
export const MASKED_QTY = "yyy";

// For free-text markdown (vault campaign bodies, journal entries): replace
// every dollar amount with the mask. Over-masks fill prices quoted in prose —
// acceptable; prose can't distinguish a balance from a price reliably.
export function maskDollarsInText(text) {
  if (!text) return text;
  return text.replace(/\$\s?\d[\d,]*(\.\d+)?/g, MASKED_MONEY);
}
