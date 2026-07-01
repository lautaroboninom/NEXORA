// web/src/components/StatusChip.jsx
import { estadoLabel } from "../lib/constants";
import DataChip from "./DataChip.jsx";

function variantOf(value) {
  const s = String(value ?? "").toLowerCase();
  if (!s) return "neutral";

  // Pendiente(s)
  if (s.includes("pend")) return "amber";

  // Ingresos iniciales
  if (s.includes("ingres")) return "cyan";

  // Venta de equipos propios
  if (s.includes("vendido")) return s.includes("entregado") ? "gray" : "amber";

  // Rechazado (p.ej. Presupuesto rechazado)
  if (s.includes("rechaz")) return "rose";

  if (s.includes("no_se_repara") || s.includes("no se repara")) return "gray";
  if (s.includes("no_aplica") || s.includes("no aplica")) return "gray";

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

export default function StatusChip({ value, title, className = "" }) {
  const v = variantOf(value);
  const label = labelOf(value);

  if (!String(value ?? "").trim()) {
    return <span>-</span>;
  }

  return (
    <DataChip value={label} tone={v} dot title={title || label} className={className} />
  );
}
