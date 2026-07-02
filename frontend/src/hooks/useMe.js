import { useQuery } from "@tanstack/react-query";
import { getMe } from "../api/me";

export function useMe() {
  return useQuery({
    queryKey: ["me"],
    queryFn: getMe,
    staleTime: Infinity,
  });
}

// Returns "owner" | "guest" | undefined (while loading).
export function useRole() {
  const { data } = useMe();
  return data?.role;
}
