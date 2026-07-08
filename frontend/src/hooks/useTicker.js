import { useQuery } from "@tanstack/react-query";
import { analyzeTicker } from "../api/scanner";
import { getTickerContext, getTickerNarrative } from "../api/ticker";
import { getWatchlist } from "../api/watchlist";

export function useTickerAnalysis(symbol) {
  return useQuery({
    queryKey: ["ticker", symbol],
    queryFn: () => analyzeTicker(symbol),
    enabled: !!symbol,
    staleTime: 60 * 1000,
  });
}

export function useTickerWatchlistItem(symbol) {
  return useQuery({
    queryKey: ["watchlist"],
    queryFn: () => getWatchlist(),
    select: (data) => data.find((item) => item.ticker === symbol?.toUpperCase()),
    enabled: !!symbol,
  });
}

export function useTickerContext(symbol) {
  return useQuery({
    queryKey: ["tickerContext", symbol],
    queryFn: () => getTickerContext(symbol),
    enabled: !!symbol,
    staleTime: 20 * 60 * 1000, // 20 min — backend caches 24h, FMP data changes slowly
    retry: false,
  });
}

export function useTickerNarrative(symbol) {
  return useQuery({
    queryKey: ["tickerNarrative", symbol],
    queryFn: () => getTickerNarrative(symbol),
    enabled: !!symbol,
    staleTime: 20 * 60 * 1000,
    retry: false,
  });
}
