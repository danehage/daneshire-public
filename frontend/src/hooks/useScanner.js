import { useState, useEffect, useCallback } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  getUniverses,
  executeScan,
  getScanResults,
  getLatestScan,
  analyzeTicker,
  createScanStream,
} from "../api/scanner";

export function useUniverses() {
  return useQuery({
    queryKey: ["universes"],
    queryFn: getUniverses,
    staleTime: 1000 * 60 * 60, // 1 hour - universes don't change often
  });
}

// The last scan saved to the database (#126): what the page shows when no
// scan has been run in this session. data is null when none is saved yet.
export function useLatestScan() {
  return useQuery({
    queryKey: ["scanLatest"],
    queryFn: getLatestScan,
    // Short: a scan finishing in another tab or session should show up.
    staleTime: 1000 * 30,
    retry: false,
  });
}

export function useAnalyzeTicker(symbol) {
  return useQuery({
    queryKey: ["analyze", symbol],
    queryFn: () => analyzeTicker(symbol),
    enabled: !!symbol,
  });
}

export function useScan() {
  const [scanId, setScanId] = useState(null);
  const [progress, setProgress] = useState(null);
  const [isScanning, setIsScanning] = useState(false);
  const [error, setError] = useState(null);
  const queryClient = useQueryClient();

  // Start scan mutation
  const startScan = useMutation({
    mutationFn: executeScan,
    onSuccess: (data) => {
      setScanId(data.scan_id);
      setIsScanning(true);
      setError(null);
      setProgress({
        stage: "quotes",
        current: 0,
        total: data.universe_size,
        found: 0,
      });
      // Starting a scan may have (re)built the screener universe (#118);
      // refresh the picker's sizes.
      queryClient.invalidateQueries({ queryKey: ["universes"] });
    },
    onError: (err) => {
      setError(err.message);
      setIsScanning(false);
    },
  });

  // SSE progress tracking
  useEffect(() => {
    if (!scanId || !isScanning) return;

    const eventSource = createScanStream(scanId);

    eventSource.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);

        if (data.type === "progress") {
          setProgress({
            stage: data.stage,
            current: data.current,
            total: data.total,
            found: data.found,
            errors: data.errors || 0,
          });
        } else if (data.type === "complete") {
          setIsScanning(false);
          // Invalidate to fetch results
          queryClient.invalidateQueries({ queryKey: ["scanResults", scanId] });
          // The finished scan is now the latest saved one.
          queryClient.invalidateQueries({ queryKey: ["scanLatest"] });
          eventSource.close();
        } else if (data.type === "error") {
          setError(data.error);
          setIsScanning(false);
          eventSource.close();
        }
      } catch (e) {
        console.error("Failed to parse SSE event:", e);
      }
    };

    eventSource.onerror = () => {
      setError("Connection lost");
      setIsScanning(false);
      eventSource.close();
    };

    return () => {
      eventSource.close();
    };
  }, [scanId, isScanning, queryClient]);

  // Fetch results: partial snapshots every 10s while the scan runs
  // (backend returns partial=true, score-sorted), final on completion.
  const { data: results, isLoading: resultsLoading } = useQuery({
    queryKey: ["scanResults", scanId],
    queryFn: () => getScanResults(scanId),
    enabled: !!scanId,
    refetchInterval: isScanning ? 10 * 1000 : false,
    retry: false,
  });

  const reset = useCallback(() => {
    setScanId(null);
    setProgress(null);
    setIsScanning(false);
    setError(null);
  }, []);

  return {
    startScan: startScan.mutate,
    isStarting: startScan.isPending,
    scanId,
    progress,
    isScanning,
    results,
    resultsLoading,
    error,
    reset,
  };
}
