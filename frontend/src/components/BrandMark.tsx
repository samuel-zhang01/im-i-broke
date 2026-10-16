/** Decorative brand artwork; the adjacent wordmark supplies the accessible name. */
export default function BrandMark({ className = "h-9 w-9" }: { className?: string }) {
  return <img
    src="/assets/brand/mark-64-v1.png"
    srcSet="/assets/brand/mark-64-v1.png 1x, /assets/brand/mark-128-v1.png 2x"
    width={64}
    height={64}
    alt=""
    aria-hidden="true"
    draggable={false}
    className={`shrink-0 rounded-lg ${className}`}
  />;
}
