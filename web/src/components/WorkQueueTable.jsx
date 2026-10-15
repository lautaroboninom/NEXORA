import { useMemo } from "react";

import CustomerCell from "./CustomerCell.jsx";
import DataChip from "./DataChip.jsx";
import StatusChip from "./StatusChip.jsx";
import DeviceIdentifier from "./DeviceIdentifier.jsx";
import { DerivadoMark } from "./DerivadoBadge.jsx";
import IngresoTable from "./IngresoTable.jsx";
import {
  formatDateOnly,
  formatOS,
  ingresoIdOf,
  isMotivoCotizacionEquipo,
  modeloSerieVarianteOf,
  resolveFechaIngreso,
  tipoEquipoOf,
} from "../lib/ui-helpers";
import {
  WORK_QUEUE_SORT_COLUMNS,
  workQueueAgeDays,
  workQueuePriority,
  workQueueProperty,
  workQueueTechnician,
} from "../lib/work-queue";
import { estadoLabel } from "../lib/constants";

function normalizedOptions(options) {
  return (options || []).map((option) =>
    typeof option === "string" ? { value: option, label: option } : option
  );
}

// workQueuePriority habla de "red"; en la paleta de los chips ese rojo es "rose".
const PRIORITY_TONES = { red: "rose", amber: "amber", blue: "blue", gray: "gray" };

function PriorityCell({ row }) {
  const priority = workQueuePriority(row);
  const days = workQueueAgeDays(row);
  return (
    <div className="inline-flex flex-col items-start gap-1">
      <DataChip value={priority.label} tone={PRIORITY_TONES[priority.tone] || "gray"} />
      <span
        className="whitespace-nowrap text-xs text-gray-600"
        title={formatDateOnly(resolveFechaIngreso(row))}
      >
        {days == null ? "-" : `${days} días`}
      </span>
    </div>
  );
}

function OsCell({ row }) {
  return (
    <>
      {formatOS(row)}
      <DerivadoMark row={row} className="ml-1.5 align-middle" />
      {isMotivoCotizacionEquipo(row?.motivo) && (
        <span className="ml-2 inline-block px-2 py-0.5 text-[10px] rounded bg-amber-100 text-amber-800 align-middle">
          Cotización
        </span>
      )}
    </>
  );
}

/**
 * Cola de trabajo (Pendientes general, Mis pendientes, Pendientes por técnico).
 *
 * Es la definición de columnas de esas tres páginas; el armado de la tabla lo
 * hace IngresoTable, igual que el resto de las listas de Servicio Técnico.
 */
export default function WorkQueueTable({
  rows,
  loading = false,
  emptyText = "No hay trabajos para mostrar.",
  onOpen,
  showTechnician = true,
  sort = "",
  onSortChange,
  showFilters = false,
  filters = {},
  onFilterChange,
  filterOptions = {},
  totalCount,
  queueHighlight,
}) {
  const columns = useMemo(() => {
    const list = [
      {
        field: "prioridad",
        label: "Prioridad",
        headerClassName: "min-w-28",
        render: (row) => <PriorityCell row={row} />,
        parts: [
          { field: "prioridad", label: "Prioridad", filter: "select" },
          { field: "antiguedad", label: "Antigüedad", filter: "select" },
        ],
      },
      {
        field: "os",
        label: "OS",
        cellClassName: "font-medium text-gray-900 underline",
        render: (row) => <OsCell row={row} />,
        filter: "text",
      },
      {
        field: "cliente",
        label: "Cliente",
        cellClassName: "min-w-44",
        render: (row) => <CustomerCell row={row} />,
        filter: "text",
      },
      { field: "propiedad", label: "Propiedad", cellClassName: "whitespace-nowrap", filter: "select" },
      {
        field: "tipo",
        label: "Tipo",
        cellClassName: "whitespace-nowrap",
        render: (row) => tipoEquipoOf(row) || "-",
        filter: "select",
      },
      { field: "marca", label: "Marca", cellClassName: "whitespace-nowrap", filter: "select" },
      {
        field: "modelo",
        label: "Modelo + Variante",
        cellClassName: "whitespace-nowrap",
        render: (row) => modeloSerieVarianteOf(row, "-"),
        filter: "select",
      },
      {
        field: "identificacion",
        label: "N/S o MG",
        cellClassName: "whitespace-nowrap",
        render: (row) => <DeviceIdentifier row={row} />,
        filter: "text",
      },
    ];

    if (showTechnician) {
      list.push({
        field: "tecnico",
        label: "Técnico",
        cellClassName: "whitespace-nowrap",
        render: (row) => workQueueTechnician(row) || "-",
        filter: "select",
      });
    }

    list.push(
      {
        field: "motivo",
        label: "Motivo",
        cellClassName: "min-w-36",
        render: (row) => row?.motivo || "-",
        filter: "select",
      },
      {
        field: "estado",
        label: "Estado",
        render: (row) => <StatusChip value={row?.estado} />,
        filter: "select",
        filterLabel: (value) => estadoLabel(value),
      },
      {
        field: "presupuesto",
        label: "Presupuesto",
        render: (row) => <StatusChip value={row?.presupuesto_estado} />,
        filter: "select",
        filterLabel: (value) => estadoLabel(value),
      }
    );

    return list;
  }, [showTechnician]);

  // La cola trae sus opciones de filtro ya armadas desde la página, así que se
  // respetan tal cual en vez de derivarlas de las filas visibles.
  const resolvedFilterOptions = useMemo(() => {
    const resolved = {};
    for (const [field, options] of Object.entries(filterOptions || {})) {
      resolved[field] = normalizedOptions(options);
    }
    return resolved;
  }, [filterOptions]);

  return (
    <IngresoTable
      queueHighlight={queueHighlight}
      rows={rows}
      columns={columns}
      sortColumns={WORK_QUEUE_SORT_COLUMNS}
      sort={sort}
      onSortChange={onSortChange}
      onOpen={onOpen}
      openLabel={(row) => `Abrir trabajo ${formatOS(row)}`}
      filters={filters}
      onFilterChange={onFilterChange}
      filterOptions={resolvedFilterOptions}
      showFilters={showFilters}
      loading={loading}
      emptyText={emptyText}
      totalCount={totalCount}
      countLabel="trabajos"
      rowKey={(row) =>
        ingresoIdOf(row) ||
        `${row?.alert_key || "row"}-${row?.preventivo_plan_id || row?.device_id || ""}`
      }
    />
  );
}
