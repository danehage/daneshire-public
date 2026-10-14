import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  getDecisions,
  getDecisionSummary,
  createDecision,
  deleteDecision,
} from "../api/decisions";

export function useDecisions({ ticker, decision, limit } = {}) {
  return useQuery({
    queryKey: ["decisions", { ticker: ticker ?? null, decision: decision ?? null, limit: limit ?? null }],
    queryFn: () => getDecisions({ ticker, decision, limit }),
  });
}

export function useDecisionSummary() {
  return useQuery({
    queryKey: ["decisions", "summary"],
    queryFn: getDecisionSummary,
  });
}

// Decisions also show in the journal feed and the dashboard's recent
// activity, so refresh those too.
function invalidateAll(queryClient) {
  queryClient.invalidateQueries({ queryKey: ["decisions"] });
  queryClient.invalidateQueries({ queryKey: ["journal-all"] });
  queryClient.invalidateQueries({ queryKey: ["dashboard"] });
}

export function useCreateDecision() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: createDecision,
    onSuccess: () => invalidateAll(queryClient),
  });
}

export function useDeleteDecision() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: deleteDecision,
    onSuccess: () => invalidateAll(queryClient),
  });
}
