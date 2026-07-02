import { Navigate } from "react-router-dom";
import { useMe } from "../hooks/useMe";

// Redirects guests to the scanner; renders nothing until the role is known
// so owner-only pages never fire their queries as a guest.
export default function OwnerRoute({ children }) {
  const { data, isLoading } = useMe();

  if (isLoading) return null;
  if (data?.role === "guest") return <Navigate to="/scanner" replace />;
  return children;
}
