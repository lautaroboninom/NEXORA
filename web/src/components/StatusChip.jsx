// web/src/components/StatusChip.jsx
// Chip de estado con variantes de color discretas y accesibles
import { estadoLabel } from "../lib/constants";

const VARIANTS = {
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
  blue: {
    wrapper: "bg-blue-50 text-blue-800 ring-1 ring-inset ring-blue-200",
    dot: "bg-blue-500",
  },
  indigo: {
    wrapper: "bg-indigo-50 text-indigo-800 ring-1 ring-inset ring-indigo-200",
    dot: "bg-indigo-500",
  },
  cyan: {
    wrapper: "bg-cyan-50 text-cyan-800 ring-1 ring-inset ring-cyan-200",
    dot: "bg-cyan-500",
  },
  rose: {
    wrapper: "bg-rose-50 text-rose-800 ring-1 ring-inset ring-rose-200",
    dot: "bg-rose-600",
  },
  gray: {
    wrapper: "bg-gray-100 text-gray-800 ring-1 ring-inset ring-gray-300",
    dot: "bg-gray-500",
  },
  neutral: {
    wrapper: "bg-gray-50 text-gray-700 ring-1 ring-inset ring-gray-200",
    dot: "bg-gray-400",
  },
};

function variantOf(value) {
  const s = String(value ?? "").toLowerCase();
  if (!s) return "neutral";

  // Pendiente(s)
  if (s.includes("pend")) return "amber";

  // Venta de equipos propios
  if (s.includes("vendido")) return s.includes("entregado") ? "gray" : "amber";

  // Rechazado (p.ej. Presupuesto rechazado)
  if (s.includes("rechaz")) return "rose";

  // Aprobados p/reparar (aprobado | reparar)
  if (s.includes("aprob") || s.includes("reparar")) return "green";

  // Derivados
  if (s.includes("deriv")) return "blue";

  // Controlado sin defecto (equipos propios revisados sin falla)
  if (s.includes("controlado")) return "blue";

  // Reparados
  if (s.includes("reparad")) return "gray";

  // Presupuesto (presupuestado, pendiente de presupuesto)
  if (s.includes("presu")) return "lime";

  // Liberados
  if (s.includes("liberad")) return "indigo";

  // Baja (equipo desguazado/obsoleto)
  if (s.includes("baja")) return "gray";

  // Cambio (resolución)
  if (s.includes("cambio")) return "purple";

  return "neutral";
}

function labelOf(value) {
  return estadoLabel(value) || "-";
}

export default function StatusChip({ value, title }) {
  const v = variantOf(value);
  const classes = VARIANTS[v] || VARIANTS.neutral;
  const label = labelOf(value);

  if (!String(value ?? "").trim()) {
    return <span>-</span>;
  }

  return (
    <span
      className={`inline-flex items-center gap-1 px-2 py-0.5 text-[11px] font-medium rounded-full ${classes.wrapper}`}
      title={title || label}
      aria-label={title || label}
    >
      <span className={`inline-block w-1.5 h-1.5 rounded-full ${classes.dot}`} />
      <span>{label}</span>
    </span>
  );
}
