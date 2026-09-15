import DataChip from "./DataChip.jsx";
import { formatDateOnly } from "../lib/ui-helpers.js";

export function derivationTitle(row = {}) {
  if (!row?.derivacion_activa) return "";
  const parts = ["Derivado"];
  if (row?.derivacion_proveedor) parts.push(row.derivacion_proveedor);
  if (row?.derivacion_fecha) parts.push(`desde ${formatDateOnly(row.derivacion_fecha)}`);
  return parts.join(" · ");
}

export default function DerivadoBadge({ row, className = "" }) {
  if (!row?.derivacion_activa) return null;
  return (
    <DataChip
      value="Derivado"
      tone="cyan"
      dot
      title={derivationTitle(row)}
      className={`no-underline ${className}`.trim()}
    />
  );
}

/**
 * Marca compacta para los listados: una sola "D" pegada al número de OS.
 * El nombre completo (proveedor y fecha incluidos) queda en el tooltip y en el
 * texto accesible, para no robarle ancho a la columna.
 */
export function DerivadoMark({ row, className = "" }) {
  if (!row?.derivacion_activa) return null;
  return (
    <span
      className={`inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-full bg-cyan-50 text-[10px] no-underline font-semibold leading-none text-cyan-800 ring-1 ring-inset ring-cyan-200 ${className}`.trim()}
      title={derivationTitle(row)}
      aria-label={derivationTitle(row)}
    >
      D
    </span>
  );
}
