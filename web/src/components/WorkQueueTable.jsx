import { ArrowDown, ArrowUp, ArrowUpDown } from "lucide-react";
import StatusChip from "./StatusChip.jsx";
import DeviceIdentifier from "./DeviceIdentifier.jsx";
import {
  catalogEquipmentLabel,
  formatDateOnly,
  formatOS,
  ingresoIdOf,
  isMotivoCotizacionEquipo,
  resolveFechaIngreso,
  tipoEquipoOf,
} from "../lib/ui-helpers";
import {
  WORK_QUEUE_SORT_COLUMNS,
  WORK_QUEUE_SORT_OPTIONS,
  workQueueAgeDays,
  workQueueNextAction,
  workQueuePriority,
  workQueueProperty,
  workQueueTechnician,
} from "../lib/work-queue";
import { nextSort, parseSort, sortRows } from "../lib/table-sort";
import { MobileSortControl, SortableTh } from "./TableSortControls.jsx";
import {
  DesktopTableWrap,
  MobileDataCard,
  MobileDataField,
  MobileDataList,
} from "./Responsive.jsx";

const toneClasses = {
  red: "bg-red-50 text-red-700 ring-red-200",
  amber: "bg-amber-50 text-amber-800 ring-amber-200",
  blue: "bg-blue-50 text-blue-700 ring-blue-200",
  gray: "bg-gray-50 text-gray-700 ring-gray-200",
};

function normalizedOptions(options) {
  return (options || []).map((option) =>
    typeof option === "string" ? { value: option, label: option } : option
  );
}

function QueueSortButton({ field, label, sort, onSortChange }) {
  if (typeof onSortChange !== "function") {
    return <span>{label}</span>;
  }

  const parsed = parseSort(sort);
  const active = parsed.field === field;
  const Icon = !active ? ArrowUpDown : parsed.direction === "desc" ? ArrowDown : ArrowUp;

  return (
    <button
      type="button"
      onClick={() => onSortChange(nextSort(sort, field))}
      className="inline-flex w-full items-center gap-1 text-left hover:text-gray-900 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500"
      aria-label={`Ordenar por ${label.toLowerCase()}`}
      title={`Ordenar por ${label.toLowerCase()}`}
    >
      <span>{label}</span>
      <Icon
        aria-hidden="true"
        className={`h-3.5 w-3.5 shrink-0 ${active ? "text-blue-700" : "text-gray-400"}`}
      />
    </button>
  );
}

function QueueFilterInput({ label, value, onChange, placeholder }) {
  return (
    <label className="text-[11px] font-medium text-gray-600">
      <span className="md:hidden">{label}</span>
      <input
        type="text"
        value={value || ""}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder || label}
        aria-label={`Filtrar ${label.toLowerCase()}`}
        className="mt-1 w-full min-w-24 rounded border border-gray-300 bg-white p-1.5 text-xs text-gray-900 md:mt-0"
      />
    </label>
  );
}

function QueueFilterSelect({ label, value, onChange, options, allLabel = "Todos" }) {
  return (
    <label className="text-[11px] font-medium text-gray-600">
      <span className="md:hidden">{label}</span>
      <select
        value={value || ""}
        onChange={(event) => onChange(event.target.value)}
        aria-label={`Filtrar ${label.toLowerCase()}`}
        className="mt-1 w-full min-w-24 rounded border border-gray-300 bg-white p-1.5 text-xs text-gray-900 md:mt-0"
      >
        <option value="">{allLabel}</option>
        {normalizedOptions(options).map((option) => (
          <option key={`${label}-${option.value}`} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
  );
}

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
}) {
  const data = Array.isArray(rows) ? rows : [];
  const sortedData = sortRows(data, sort, WORK_QUEUE_SORT_COLUMNS);
  const updateFilter = (field) => (value) => onFilterChange?.(field, value);

  const openRow = (row) => {
    if (typeof onOpen === "function") onOpen(row);
  };

  if (loading) {
    return <div className="text-sm text-gray-500 py-4">Cargando...</div>;
  }

  if (!data.length && !showFilters && typeof onSortChange !== "function") {
    return <div className="text-sm text-gray-500 py-4">{emptyText}</div>;
  }

  return (
    <div>
      {showFilters && (
        <div className="mb-3 rounded-md border border-slate-200 bg-slate-50 p-3 md:hidden">
          <div className="mb-2 text-sm font-semibold text-slate-800">Filtros avanzados</div>
          <div className="grid grid-cols-1 gap-2 min-[420px]:grid-cols-2">
            <QueueFilterSelect label="Prioridad" value={filters.prioridad} onChange={updateFilter("prioridad")} options={filterOptions.prioridad} />
            <QueueFilterInput label="OS" value={filters.os} onChange={updateFilter("os")} />
            <QueueFilterSelect label="Antigüedad" value={filters.antiguedad} onChange={updateFilter("antiguedad")} options={filterOptions.antiguedad} />
            <QueueFilterInput label="Cliente" value={filters.cliente} onChange={updateFilter("cliente")} />
            <QueueFilterSelect label="Propiedad" value={filters.propiedad} onChange={updateFilter("propiedad")} options={filterOptions.propiedad} />
            <QueueFilterSelect label="Tipo" value={filters.tipo} onChange={updateFilter("tipo")} options={filterOptions.tipo} />
            <QueueFilterSelect label="Marca" value={filters.marca} onChange={updateFilter("marca")} options={filterOptions.marca} />
            <QueueFilterSelect label="Modelo" value={filters.modelo} onChange={updateFilter("modelo")} options={filterOptions.modelo} />
            <QueueFilterSelect label="Variante" value={filters.variante} onChange={updateFilter("variante")} options={filterOptions.variante} />
            <QueueFilterInput label="N/S o MG" value={filters.identificacion} onChange={updateFilter("identificacion")} />
            {showTechnician && <QueueFilterSelect label="Técnico" value={filters.tecnico} onChange={updateFilter("tecnico")} options={filterOptions.tecnico} />}
            <QueueFilterSelect label="Motivo" value={filters.motivo} onChange={updateFilter("motivo")} options={filterOptions.motivo} />
            <QueueFilterSelect label="Estado" value={filters.estado} onChange={updateFilter("estado")} options={filterOptions.estado} />
            <QueueFilterSelect label="Presupuesto" value={filters.presupuesto} onChange={updateFilter("presupuesto")} options={filterOptions.presupuesto} />
            <QueueFilterSelect label="Próxima acción" value={filters.accion} onChange={updateFilter("accion")} options={filterOptions.accion} />
          </div>
        </div>
      )}

      {typeof onSortChange === "function" && (
        <MobileSortControl
          sort={sort}
          onSortChange={onSortChange}
          options={WORK_QUEUE_SORT_OPTIONS.filter((option) => showTechnician || option.value !== "tecnico")}
          className="mb-3"
        />
      )}

      {!sortedData.length && (
        <div className="py-4 text-sm text-gray-500">{emptyText}</div>
      )}

      <MobileDataList>
        {sortedData.map((row, index) => {
          const id = ingresoIdOf(row);
          const priority = workQueuePriority(row);
          const days = workQueueAgeDays(row);
          const priorityClass = toneClasses[priority.tone] || toneClasses.gray;
          const esCotizacion = isMotivoCotizacionEquipo(row?.motivo);
          return (
            <MobileDataCard
              key={id || `${row?.alert_key || "row"}-${row?.preventivo_plan_id || row?.device_id || index}`}
              as="button"
              type="button"
              onClick={() => openRow(row)}
              className="hover:bg-gray-50"
              aria-label={`Abrir trabajo ${formatOS(row)}`}
            >
              <div className="flex items-start justify-between gap-3">
                <div>
                  <div className="font-semibold text-gray-900 underline">{formatOS(row)}</div>
                  {esCotizacion && (
                    <span className="mt-1 inline-flex px-2 py-0.5 text-[10px] rounded bg-amber-100 text-amber-800">
                      Cotización
                    </span>
                  )}
                </div>
                <span className={`inline-flex shrink-0 items-center px-2 py-0.5 rounded-full text-[11px] font-medium ring-1 ring-inset ${priorityClass}`}>
                  {priority.label}
                </span>
              </div>
              <div className="mt-3 grid grid-cols-1 gap-2 min-[420px]:grid-cols-2">
                <MobileDataField label="Cliente" value={row?.razon_social ?? row?.cliente ?? row?.cliente_nombre ?? "-"} />
                <MobileDataField label="Equipo" value={catalogEquipmentLabel(row)} />
                <MobileDataField label="Propiedad" value={workQueueProperty(row)} />
                <MobileDataField label="N/S o MG">
                  <DeviceIdentifier row={row} />
                </MobileDataField>
                {showTechnician && <MobileDataField label="Técnico" value={workQueueTechnician(row) || "-"} />}
                <MobileDataField label="Motivo" value={row?.motivo || "-"} />
                <MobileDataField label="Antigüedad" value={days == null ? "-" : `${days} días`} />
                <MobileDataField label="Estado">
                  <StatusChip value={row?.estado} />
                </MobileDataField>
                <MobileDataField label="Presupuesto">
                  <StatusChip value={row?.presupuesto_estado} />
                </MobileDataField>
                <MobileDataField label="Próxima acción" value={workQueueNextAction(row)} className="min-[420px]:col-span-2" />
              </div>
            </MobileDataCard>
          );
        })}
      </MobileDataList>

      <DesktopTableWrap>
      <table className="min-w-full text-sm border-separate border-spacing-0">
        <thead>
          <tr className="text-left text-xs uppercase tracking-wide text-gray-500">
            <th scope="col" className="min-w-28 border-b p-2">
              <div className="flex flex-col gap-1">
                <QueueSortButton field="prioridad" label="Prioridad" sort={sort} onSortChange={onSortChange} />
                <QueueSortButton field="antiguedad" label="Antigüedad" sort={sort} onSortChange={onSortChange} />
              </div>
            </th>
            <SortableTh field="os" sort={sort} onSortChange={onSortChange} className="p-2 border-b">OS</SortableTh>
            <SortableTh field="cliente" sort={sort} onSortChange={onSortChange} className="p-2 border-b">Cliente</SortableTh>
            <SortableTh field="propiedad" sort={sort} onSortChange={onSortChange} className="p-2 border-b">Propiedad</SortableTh>
            <SortableTh field="tipo" sort={sort} onSortChange={onSortChange} className="p-2 border-b">Tipo</SortableTh>
            <SortableTh field="marca" sort={sort} onSortChange={onSortChange} className="p-2 border-b">Marca</SortableTh>
            <SortableTh field="modelo" sort={sort} onSortChange={onSortChange} className="p-2 border-b">Modelo</SortableTh>
            <SortableTh field="variante" sort={sort} onSortChange={onSortChange} className="p-2 border-b">Variante</SortableTh>
            <SortableTh field="identificacion" sort={sort} onSortChange={onSortChange} className="p-2 border-b">N/S o MG</SortableTh>
            {showTechnician && <SortableTh field="tecnico" sort={sort} onSortChange={onSortChange} className="p-2 border-b">Técnico</SortableTh>}
            <SortableTh field="motivo" sort={sort} onSortChange={onSortChange} className="p-2 border-b">Motivo</SortableTh>
            <SortableTh field="estado" sort={sort} onSortChange={onSortChange} className="p-2 border-b">Estado</SortableTh>
            <SortableTh field="presupuesto" sort={sort} onSortChange={onSortChange} className="p-2 border-b">Presupuesto</SortableTh>
            <SortableTh field="accion" sort={sort} onSortChange={onSortChange} className="p-2 border-b">Próxima acción</SortableTh>
          </tr>
          {showFilters && (
            <tr className="bg-slate-50 align-top">
              <th className="border-b p-1">
                <div className="flex min-w-28 flex-col gap-1.5">
                  <QueueFilterSelect label="Prioridad" value={filters.prioridad} onChange={updateFilter("prioridad")} options={filterOptions.prioridad} />
                  <QueueFilterSelect label="Antigüedad" value={filters.antiguedad} onChange={updateFilter("antiguedad")} options={filterOptions.antiguedad} />
                </div>
              </th>
              <th className="p-1 border-b"><QueueFilterInput label="OS" value={filters.os} onChange={updateFilter("os")} /></th>
              <th className="p-1 border-b"><QueueFilterInput label="Cliente" value={filters.cliente} onChange={updateFilter("cliente")} /></th>
              <th className="p-1 border-b"><QueueFilterSelect label="Propiedad" value={filters.propiedad} onChange={updateFilter("propiedad")} options={filterOptions.propiedad} /></th>
              <th className="p-1 border-b"><QueueFilterSelect label="Tipo" value={filters.tipo} onChange={updateFilter("tipo")} options={filterOptions.tipo} /></th>
              <th className="p-1 border-b"><QueueFilterSelect label="Marca" value={filters.marca} onChange={updateFilter("marca")} options={filterOptions.marca} /></th>
              <th className="p-1 border-b"><QueueFilterSelect label="Modelo" value={filters.modelo} onChange={updateFilter("modelo")} options={filterOptions.modelo} /></th>
              <th className="p-1 border-b"><QueueFilterSelect label="Variante" value={filters.variante} onChange={updateFilter("variante")} options={filterOptions.variante} /></th>
              <th className="p-1 border-b"><QueueFilterInput label="N/S o MG" value={filters.identificacion} onChange={updateFilter("identificacion")} /></th>
              {showTechnician && <th className="p-1 border-b"><QueueFilterSelect label="Técnico" value={filters.tecnico} onChange={updateFilter("tecnico")} options={filterOptions.tecnico} /></th>}
              <th className="p-1 border-b"><QueueFilterSelect label="Motivo" value={filters.motivo} onChange={updateFilter("motivo")} options={filterOptions.motivo} /></th>
              <th className="p-1 border-b"><QueueFilterSelect label="Estado" value={filters.estado} onChange={updateFilter("estado")} options={filterOptions.estado} /></th>
              <th className="p-1 border-b"><QueueFilterSelect label="Presupuesto" value={filters.presupuesto} onChange={updateFilter("presupuesto")} options={filterOptions.presupuesto} /></th>
              <th className="p-1 border-b"><QueueFilterSelect label="Próxima acción" value={filters.accion} onChange={updateFilter("accion")} options={filterOptions.accion} /></th>
            </tr>
          )}
        </thead>
        <tbody>
          {sortedData.map((row, index) => {
            const id = ingresoIdOf(row);
            const priority = workQueuePriority(row);
            const days = workQueueAgeDays(row);
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
                  <div className="inline-flex flex-col items-start gap-1">
                    <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-medium ring-1 ring-inset ${priorityClass}`}>
                      {priority.label}
                    </span>
                    <span
                      className="whitespace-nowrap text-xs text-gray-600"
                      title={formatDateOnly(resolveFechaIngreso(row))}
                    >
                      {days == null ? "-" : `${days} días`}
                    </span>
                  </div>
                </td>
                <td className="p-2 border-b align-top font-medium text-gray-900 underline">
                  {formatOS(row)}
                  {esCotizacion && (
                    <span className="ml-2 inline-block px-2 py-0.5 text-[10px] rounded bg-amber-100 text-amber-800 align-middle">
                      Cotización
                    </span>
                  )}
                </td>
                <td className="p-2 border-b align-top min-w-44">
                  {row?.razon_social ?? row?.cliente ?? row?.cliente_nombre ?? "-"}
                </td>
                <td className="p-2 border-b align-top whitespace-nowrap">{workQueueProperty(row)}</td>
                <td className="p-2 border-b align-top whitespace-nowrap">{tipoEquipoOf(row) || "-"}</td>
                <td className="p-2 border-b align-top whitespace-nowrap">{row?.marca ?? row?.equipo?.marca ?? "-"}</td>
                <td className="p-2 border-b align-top whitespace-nowrap">{row?.modelo ?? row?.equipo?.modelo ?? "-"}</td>
                <td className="p-2 border-b align-top whitespace-nowrap">{row?.equipo_variante ?? row?.variante ?? row?.equipo?.variante ?? "-"}</td>
                <td className="p-2 border-b align-top whitespace-nowrap">
                  <DeviceIdentifier row={row} />
                </td>
                {showTechnician && (
                  <td className="p-2 border-b align-top whitespace-nowrap">{workQueueTechnician(row) || "-"}</td>
                )}
                <td className="p-2 border-b align-top min-w-36">{row?.motivo || "-"}</td>
                <td className="p-2 border-b align-top">
                  <StatusChip value={row?.estado} />
                </td>
                <td className="p-2 border-b align-top">
                  <StatusChip value={row?.presupuesto_estado} />
                </td>
                <td className="p-2 border-b align-top min-w-44 text-gray-700">{workQueueNextAction(row)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      </DesktopTableWrap>
      <div className="text-xs text-gray-500 mt-2">
        Mostrando {sortedData.length}{Number.isFinite(totalCount) ? ` de ${totalCount}` : ""} trabajos.
      </div>
    </div>
  );
}
