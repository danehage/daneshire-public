import { useQuery } from "@tanstack/react-query";
import { getTradeCampaign, getTradeCampaigns } from "../api/tradeCampaigns";

export function useTradeCampaigns(options = {}) {
  return useQuery({
    queryKey: ["tradeCampaigns", "list", options],
    queryFn: () => getTradeCampaigns(options),
  });
}

export function useTradeCampaign(campaignId) {
  return useQuery({
    queryKey: ["tradeCampaigns", "detail", campaignId],
    queryFn: () => getTradeCampaign(campaignId),
    enabled: Boolean(campaignId),
  });
}
