const TONES = {
  neutral: {
    wrapper: "bg-gray-50 text-gray-700 ring-1 ring-inset ring-gray-200",
    dot: "bg-gray-400",
  },
  slate: {
    wrapper: "bg-slate-50 text-slate-700 ring-1 ring-inset ring-slate-200",
    dot: "bg-slate-400",
  },
  blue: {
    wrapper: "bg-blue-50 text-blue-800 ring-1 ring-inset ring-blue-200",
    dot: "bg-blue-500",
  },
  cyan: {
    wrapper: "bg-cyan-50 text-cyan-800 ring-1 ring-inset ring-cyan-200",
    dot: "bg-cyan-500",
  },
  indigo: {
    wrapper: "bg-indigo-50 text-indigo-800 ring-1 ring-inset ring-indigo-200",
    dot: "bg-indigo-500",
  },
  purple: {
    wrapper: "bg-purple-50 text-purple-800 ring-1 ring-inset ring-purple-200",
    dot: "bg-purple-500",
  },
  amber: {
    wrapper: "bg-amber-50 text-amber-800 ring-1 ring-inset ring-amber-200",
    dot: "bg-amber-500",
  },
  green: {
    wrapper: "bg-emerald-50 text-emerald-800 ring-1 ring-inset ring-emerald-200",
    dot: "bg-emerald-500",
  },
  lime: {
    wrapper: "bg-lime-50 text-lime-800 ring-1 ring-inset ring-lime-200",
    dot: "bg-lime-500",
  },
  rose: {
    wrapper: "bg-rose-50 text-rose-800 ring-1 ring-inset ring-rose-200",
    dot: "bg-rose-600",
  },
  gray: {
    wrapper: "bg-gray-100 text-gray-800 ring-1 ring-inset ring-gray-300",
    dot: "bg-gray-500",
  },
};

function cleanLabel(value, fallback = "-") {
  const text = String(value ?? "").trim();
  return text || fallback;
}

export function ChipGroup({ children, className = "" }) {
  return <span className={`inline-flex flex-wrap items-center gap-1.5 ${className}`.trim()}>{children}</span>;
}

export default function DataChip({
  value,
  children,
  tone = "neutral",
  dot = false,
  title,
  className = "",
  mono = false,
  fallback = "-",
}) {
  const hasChildren = children !== undefined && children !== null;
  const label = hasChildren ? "" : cleanLabel(value, fallback);
  const textTitle = title || (hasChildren ? undefined : label);
  if (!hasChildren && label === fallback && fallback === "-") return <span>-</span>;

  const classes = TONES[tone] || TONES.neutral;
  return (
    <span
      className={`inline-flex max-w-full items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium leading-5 ${mono ? "font-mono" : ""} ${classes.wrapper} ${className}`.trim()}
      title={textTitle}
      aria-label={textTitle}
    >
      {dot && <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${classes.dot}`} />}
      <span className="min-w-0 truncate">{hasChildren ? children : label}</span>
    </span>
  );
}
