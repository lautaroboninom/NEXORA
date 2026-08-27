const TONES = {
  error: "bg-red-100 border-red-300 text-red-700",
  info: "bg-blue-100 border-blue-300 text-blue-800",
  success: "bg-emerald-100 border-emerald-300 text-emerald-800",
  warning: "bg-amber-100 border-amber-300 text-amber-900",
};

/** Aviso al tope de una página. Nada abajo si no hay nada que decir. */
export default function AlertBanner({ tone = "error", children, className = "" }) {
  if (!children) return null;
  const toneClass = TONES[tone] || TONES.error;
  return (
    <div
      role={tone === "error" ? "alert" : "status"}
      className={`border p-2 rounded mb-3 ${toneClass} ${className}`}
    >
      {children}
    </div>
  );
}
