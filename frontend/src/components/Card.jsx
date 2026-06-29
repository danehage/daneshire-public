/**
 * Card — the project's brutalist surface primitive: 2px ink border, warm-white
 * ground, hard offset shadow. Centralizes the
 * `border-2 border-ink bg-warm-white shadow-hard` treatment that was previously
 * inlined across every page so the design system can't drift one card at a time.
 *
 *   <Card>…</Card>                       // default hard shadow
 *   <Card shadow="responsive">…</Card>   // sm on mobile, full on sm+ breakpoint
 *   <Card as={Link} to="/x">…</Card>     // polymorphic — render as any element
 */
const SHADOWS = {
  none: "",
  sm: "shadow-hard-sm",
  default: "shadow-hard",
  lg: "shadow-hard-lg",
  responsive: "shadow-hard-sm sm:shadow-hard",
};

export default function Card({
  as: Tag = "div",
  shadow = "default",
  className = "",
  children,
  ...props
}) {
  const shadowClass = SHADOWS[shadow] ?? SHADOWS.default;
  return (
    <Tag
      className={`border-2 border-ink bg-warm-white ${shadowClass} ${className}`}
      {...props}
    >
      {children}
    </Tag>
  );
}
