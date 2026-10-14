import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  getWatchlist,
  createWatchlistItem,
  updateWatchlistItem,
  deleteWatchlistItem,
  reorderWatchlist,
} from "../api/watchlist";

export function useWatchlist(status) {
  return useQuery({
    queryKey: ["watchlist", status],
    queryFn: () => getWatchlist(status),
  });
}

export function useCreateWatchlistItem() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: createWatchlistItem,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["watchlist"] }),
  });
}

export function useUpdateWatchlistItem() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, data }) => updateWatchlistItem(id, data),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["watchlist"] }),
  });
}

// Set a holding's open date (#137): the plateau check anchors each run on
// the `owned` watchlist row's entry_date. `item` is the ticker's active
// watchlist row, if any. A non-owned active row is promoted to owned (the
// create route would 409 on it); with no row, an owned row is created.
export function useSetHoldingOpenDate() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ ticker, item, entryDate }) => {
      if (item) {
        const data = { entry_date: entryDate };
        if (item.status !== "owned") data.status = "owned";
        return updateWatchlistItem(item.id, data);
      }
      const created = await createWatchlistItem({
        ticker,
        status: "owned",
        entry_date: entryDate,
      });
      // null = 409: a row appeared since the list loaded. Refetch and retry.
      if (created === null) {
        throw new Error(`${ticker} changed on the watchlist, try again`);
      }
      return created;
    },
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["watchlist"] }),
  });
}

export function useDeleteWatchlistItem() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: deleteWatchlistItem,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["watchlist"] }),
  });
}

export function useReorderWatchlist() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: reorderWatchlist,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["watchlist"] }),
  });
}
