import StatusChip from "./StatusChip.jsx";
import DeviceIdentifier from "./DeviceIdentifier.jsx";
import {
  catalogEquipmentLabel,
  formatDateOnly,
  formatOS,
  ingresoIdOf,
  isMotivoCotizacionEquipo,
  parseDateLocal,
  resolveFechaCreacion,
  resolveFechaIngreso,
} from "../lib/ui-helpers";

function ageDays(row) {
  const raw = resolveFechaIngreso(row) || resolveFechaCreacion(row);
  const date = parseDateLocal(raw);
  if (!date || Number.isNaN(date.getTime())) return null;
  const start = new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  return Math.max(0, Math.floor((today - start) / 86400000));
}

function technicianOf(row) {
  return (
    row?.tecnico ||
    row?.tecnico_nombre ||
    row?.asignado_a_nombre ||
    row?.asignado_nombre ||
    ""
  );
}

function priorityOf(row) {
  const days = ageDays(row);
  const motivo = String(row?.motivo || "").toLowerCase();
  const estado = String(row?.estado || "").toLowerCase();
  const presupuesto = String(row?.presupuesto_estado || "").toLowerCase();
  if (row?.derivado_devuelto) {
    return { label: "Devuelto", tone: "blue", rank: 0 };
  }
  if (motivo === "urgente control") {
    return { label: "Urgente", tone: "red", rank: 1 };
  }
  if (presupuesto === "presupuestado") {
    return { label: "Presupuesto", tone: "amber", rank: 2 };
  }
  if (!row?.asignado_a && !technicianOf(row) && estado !== "liberado") {
    return { label: "Sin técnico", tone: "amber", rank: 3 };
  }
  if (days != null && days >= 16) {
    return { label: "16+ días", tone: "red", rank: 4 };
  }
  if (days != null && days >= 7) {
    return { label: "En espera", tone: "amber", rank: 5 };
  }
  return { label: "Normal", tone: "gray", rank: 9 };
}

function nextActionOf(row) {
  const estado = String(row?.estado || "").toLowerCase();
  const presupuesto = String(row?.presupuesto_estado || "").toLowerCase();
  if (row?.next_action) return row.next_action;
  if (row?.derivado_devuelto) return "Revisar devolución";
  if (!row?.asignado_a && !technicianOf(row) && estado !== "liberado") return "Asignar técnico";
  if (estado === "ingresado") return "Diagnosticar";
  if (presupuesto === "presupuestado") return "Seguimiento del presupuesto";
  if (presupuesto === "aprobado" || estado === "reparar") return "Reparar";
  if (estado === "reparado") return "Liberar o entregar";
  if (estado === "liberado") return "Coordinar entrega";
  if (estado === "vendido_pendiente_entrega") return "Confirmar entrega de venta";
  return "Actualizar avance";
}

const toneClasses = {
  red: "bg-red-50 text-red-700 ring-red-200",
  amber: "bg-amber-50 text-amber-800 ring-amber-200",
  blue: "bg-blue-50 text-blue-700 ring-blue-200",
  gray: "bg-gray-50 text-gray-700 ring-gray-200",
};

export default function WorkQueueTable({
  rows,
  loading = false,
  emptyText = "No hay trabajos para mostrar.",
  onOpen,
  showTechnician = true,
}) {
  const data = Array.isArray(rows) ? rows : [];

  const openRow = (row) => {
    if (typeof onOpen === "function") onOpen(row);
  };

  if (loading) {
    return <div className="text-sm text-gray-500 py-4">Cargando...</div>;
  }

  if (!data.length) {
    return <div className="text-sm text-gray-500 py-4">{emptyText}</div>;
  }

  return (
    <div className="overflow-x-auto">
      <table className="min-w-full text-sm border-separate border-spacing-0">
        <thead>
          <tr className="text-left text-xs uppercase tracking-wide text-gray-500">
            <th className="p-2 border-b">Prioridad</th>
            <th className="p-2 border-b">OS</th>
            <th className="p-2 border-b">Antigüedad</th>
            <th className="p-2 border-b">Cliente</th>
            <th className="p-2 border-b">Equipo</th>
            <th className="p-2 border-b">N/S o MG</th>
            {showTechnician && <th className="p-2 border-b">Técnico</th>}
            <th className="p-2 border-b">Estado</th>
            <th className="p-2 border-b">Presupuesto</th>
            <th className="p-2 border-b">Próxima acción</th>
          </tr>
        </thead>
        <tbody>
          {data.map((row, index) => {
            const id = ingresoIdOf(row);
            const priority = priorityOf(row);
            const days = ageDays(row);
            const priorityClass = toneClasses[priority.tone] || toneClasses.gray;
            const esCotizacion = isMotivoCotizacionEquipo(row?.motivo);
            return (
              <tr
                key={id || `${row?.alert_key || "row"}-${row?.preventivo_plan_id || row?.device_id || index}`}
                className="group hover:bg-gray-50 cursor-pointer"
                onClick={() => openRow(row)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    openRow(row);
                  }
                }}
                role="link"
                tabIndex={0}
                aria-label={`Abrir trabajo ${formatOS(row)}`}
              >
                <td className="p-2 border-b align-top">
                  <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-medium ring-1 ring-inset ${priorityClass}`}>
                    {priority.label}
                  </span>
                </td>
                <td className="p-2 border-b align-top font-medium text-gray-900 underline">
                  {formatOS(row)}
                  {esCotizacion && (
                    <span className="ml-2 inline-block px-2 py-0.5 text-[10px] rounded bg-amber-100 text-amber-800 align-middle">
                      Cotización
                    </span>
                  )}
                </td>
                <td className="p-2 border-b align-top whitespace-nowrap" title={formatDateOnly(resolveFechaIngreso(row))}>
                  {days == null ? "-" : `${days} días`}
                </td>
                <td className="p-2 border-b align-top min-w-44">
                  {row?.razon_social ?? row?.cliente ?? row?.cliente_nombre ?? "-"}
                </td>
                <td className="p-2 border-b align-top min-w-56">{catalogEquipmentLabel(row)}</td>
                <td className="p-2 border-b align-top whitespace-nowrap">
                  <DeviceIdentifier row={row} />
                </td>
                {showTechnician && (
                  <td className="p-2 border-b align-top whitespace-nowrap">{technicianOf(row) || "-"}</td>
                )}
                <td className="p-2 border-b align-top">
                  <StatusChip value={row?.estado} />
                </td>
                <td className="p-2 border-b align-top">
                  <StatusChip value={row?.presupuesto_estado} />
                </td>
                <td className="p-2 border-b align-top min-w-44 text-gray-700">{nextActionOf(row)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <div className="text-xs text-gray-500 mt-2">Mostrando {data.length} trabajos.</div>
    </div>
  );
}
