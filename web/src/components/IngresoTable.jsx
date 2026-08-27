import { useMemo } from "react";

import SelectionCheckbox from "./SelectionCheckbox.jsx";
import { MobileSortControl, SortButton, SortableTh } from "./TableSortControls.jsx";
import {
  DesktopTableWrap,
  MobileDataCard,
  MobileDataField,
  MobileDataList,
} from "./Responsive.jsx";
import { ingresoIdOf } from "../lib/ui-helpers";
import { sortRows } from "../lib/table-sort";
import { INGRESO_LIST_SORT_COLUMNS } from "../lib/ingreso-list-sort";
import { columnFilterValue } from "../lib/ingreso-list-filters";

/**
 * Tabla de las listas de Servicio Técnico.
 *
 * Todas las páginas mostraban lo mismo con el mismo markup copiado: tarjetas en
 * mobile, tabla en desktop, orden por encabezado y el conteo al pie. Acá eso se
 * describe una vez y cada página aporta sus columnas y sus acciones.
 *
 * Una columna se declara así:
 *   { field, label, render, filter: "text"|"select", filterLabel, filterValue,
 *     cellClassName, headerClassName, sortable, hideOnMobile,
 *     parts: [{ field, label, filter }] }
 *
 * `parts` sirve para las celdas que apilan dos datos (Prioridad y Antigüedad):
 * el encabezado ofrece un orden por cada uno y la fila de filtros, un control
 * por cada uno.
 */

function columnParts(column) {
  return Array.isArray(column?.parts) && column.parts.length > 0
    ? column.parts
    : [column];
}

function isSortable(column, sortColumns) {
  if (column?.sortable === false) return false;
  return Boolean(column?.field && sortColumns?.[column.field]);
}

function FilterControl({ column, value, onChange, options }) {
  const label = column.label || column.field;
  if (column.filter === "select") {
    return (
      <label className="text-[11px] font-medium text-gray-600">
        <span className="md:hidden">{label}</span>
        <select
          value={value || ""}
          onChange={(event) => onChange(column.field, event.target.value)}
          aria-label={`Filtrar por ${String(label).toLowerCase()}`}
          className="mt-1 w-full min-w-24 rounded border border-gray-300 bg-white p-1.5 text-xs text-gray-900 md:mt-0"
        >
          <option value="">Todos</option>
          {(options || []).map((option) => (
            <option key={`${column.field}-${option.value}`} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </label>
    );
  }

  return (
    <label className="text-[11px] font-medium text-gray-600">
      <span className="md:hidden">{label}</span>
      <input
        type="text"
        value={value || ""}
        onChange={(event) => onChange(column.field, event.target.value)}
        placeholder={label}
        aria-label={`Filtrar por ${String(label).toLowerCase()}`}
        className="mt-1 w-full min-w-24 rounded border border-gray-300 bg-white p-1.5 text-xs text-gray-900 md:mt-0"
      />
    </label>
  );
}

export default function IngresoTable({
  rows,
  columns,
  sort = "",
  onSortChange,
  sortColumns = INGRESO_LIST_SORT_COLUMNS,
  selection,
  selectionLabel = (row) => `Seleccionar ${ingresoIdOf(row)}`,
  onOpen,
  openLabel = (row) => `Abrir ${ingresoIdOf(row)}`,
  rowActions,
  actionsLabel = "Acciones",
  filters = {},
  onFilterChange,
  filterOptions = {},
  showFilters = false,
  loading = false,
  emptyText = "No hay nada para mostrar.",
  totalCount,
  countLabel = "",
  rowKey = ingresoIdOf,
  titleField = "os",
}) {
  const data = Array.isArray(rows) ? rows : [];
  const cols = useMemo(() => (columns || []).filter(Boolean), [columns]);
  const displayRows = useMemo(
    () => sortRows(data, sort, sortColumns),
    [data, sort, sortColumns]
  );

  const sortOptions = useMemo(() => {
    const options = [];
    for (const column of cols) {
      for (const part of columnParts(column)) {
        if (isSortable(part, sortColumns)) {
          options.push({ value: part.field, label: part.label || part.field });
        }
      }
    }
    return options;
  }, [cols, sortColumns]);

  const hasFilters = useMemo(
    () => cols.some((column) => columnParts(column).some((part) => part.filter)),
    [cols]
  );
  const filtersVisible = showFilters && hasFilters && typeof onFilterChange === "function";

  const titleColumn = cols.find((column) => column.field === titleField) || cols[0];
  const bodyColumns = cols.filter((column) => column !== titleColumn);

  const cellValue = (row, column) => {
    if (typeof column.render === "function") return column.render(row);
    const value = columnFilterValue(row, column, sortColumns);
    const text = value == null || value === "" ? "-" : String(value);
    return text;
  };

  const openRow = (row) => {
    if (typeof onOpen === "function") onOpen(row);
  };

  const onRowKeyDown = (event, row) => {
    if (typeof onOpen !== "function") return;
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      openRow(row);
    }
  };

  if (loading) return <div className="py-4 text-sm text-gray-500">Cargando...</div>;

  const showSelection = Boolean(selection);
  const showActions = typeof rowActions === "function";
  const isEmpty = displayRows.length === 0;

  // Sin filas y sin nada con qué volver atrás, alcanza con el mensaje. Si en
  // cambio hay filtros puestos, los controles tienen que seguir en pantalla:
  // si no, el filtro que dejó la lista vacía no se puede sacar.
  if (isEmpty && !filtersVisible) {
    return <div className="py-4 text-sm text-gray-500">{emptyText}</div>;
  }

  return (
    <div>
      {typeof onSortChange === "function" && sortOptions.length > 0 && (
        <MobileSortControl
          sort={sort}
          onSortChange={onSortChange}
          options={sortOptions}
          className="mb-3"
        />
      )}

      {filtersVisible && (
        <div className="mb-3 rounded-md border border-slate-200 bg-slate-50 p-3 md:hidden">
          <div className="mb-2 text-sm font-semibold text-slate-800">Filtros avanzados</div>
          <div className="grid grid-cols-1 gap-2 min-[420px]:grid-cols-2">
            {cols.flatMap((column) =>
              columnParts(column)
                .filter((part) => part.filter)
                .map((part) => (
                  <FilterControl
                    key={`mobile-filter-${part.field}`}
                    column={part}
                    value={filters[part.field]}
                    onChange={onFilterChange}
                    options={filterOptions[part.field]}
                  />
                ))
            )}
          </div>
        </div>
      )}

      {isEmpty && <div className="py-4 text-sm text-gray-500">{emptyText}</div>}

      <MobileDataList>
        {displayRows.map((row, index) => {
          const id = rowKey(row) || `row-${index}`;
          return (
            <MobileDataCard
              key={id}
              onClick={onOpen ? () => openRow(row) : undefined}
              onKeyDown={onOpen ? (event) => onRowKeyDown(event, row) : undefined}
              className={onOpen ? "cursor-pointer hover:bg-gray-50" : ""}
              role={onOpen ? "link" : undefined}
              tabIndex={onOpen ? 0 : undefined}
              aria-label={onOpen ? openLabel(row) : undefined}
              data-testid={`row-mobile-${rowKey(row)}`}
            >
              <div className="flex items-start justify-between gap-3">
                <div className="font-semibold text-gray-900 underline">
                  {titleColumn ? cellValue(row, titleColumn) : null}
                </div>
                {showSelection && (
                  <span onClick={(event) => event.stopPropagation()}>
                    <SelectionCheckbox
                      checked={selection.has(rowKey(row))}
                      onChange={() => selection.toggle(rowKey(row))}
                      label={selectionLabel(row)}
                      className="h-5 w-5"
                    />
                  </span>
                )}
              </div>
              <div className="mt-3 grid grid-cols-1 gap-2 min-[420px]:grid-cols-2">
                {bodyColumns
                  .filter((column) => !column.hideOnMobile)
                  .map((column) => (
                    <MobileDataField
                      key={`mobile-${column.field}`}
                      label={column.label}
                      className={column.mobileClassName}
                    >
                      {cellValue(row, column)}
                    </MobileDataField>
                  ))}
              </div>
              {showActions && (
                <div className="mt-3" onClick={(event) => event.stopPropagation()}>
                  {rowActions(row, { mobile: true })}
                </div>
              )}
            </MobileDataCard>
          );
        })}
      </MobileDataList>

      <DesktopTableWrap>
        <table className="min-w-full text-sm">
          <thead>
            <tr className="text-left">
              {showSelection && (
                <th scope="col" className="p-2">
                  <SelectionCheckbox
                    checked={selection.allVisibleSelected}
                    indeterminate={selection.someVisibleSelected}
                    onChange={selection.toggleAllVisible}
                    label="Seleccionar todas las visibles"
                  />
                </th>
              )}
              {cols.map((column) => {
                const parts = columnParts(column);
                if (parts.length > 1) {
                  return (
                    <th
                      key={`th-${column.field}`}
                      scope="col"
                      className={`p-2 ${column.headerClassName || ""}`}
                    >
                      <div className="flex flex-col gap-1">
                        {parts.map((part) => (
                          <SortButton
                            key={`th-part-${part.field}`}
                            field={part.field}
                            sort={sort}
                            onSortChange={isSortable(part, sortColumns) ? onSortChange : undefined}
                          >
                            {part.label}
                          </SortButton>
                        ))}
                      </div>
                    </th>
                  );
                }
                return (
                  <SortableTh
                    key={`th-${column.field}`}
                    field={column.field}
                    sort={sort}
                    onSortChange={isSortable(column, sortColumns) ? onSortChange : undefined}
                    className={`p-2 ${column.headerClassName || ""}`}
                  >
                    {column.label}
                  </SortableTh>
                );
              })}
              {showActions && (
                <th scope="col" className="p-2 text-right">
                  {actionsLabel}
                </th>
              )}
            </tr>
            {filtersVisible && (
              <tr className="bg-slate-50 align-top">
                {showSelection && <th className="border-b p-1" />}
                {cols.map((column) => {
                  const parts = columnParts(column).filter((part) => part.filter);
                  return (
                    <th key={`filter-${column.field}`} className="border-b p-1">
                      {parts.length > 0 ? (
                        <div className="flex flex-col gap-1.5">
                          {parts.map((part) => (
                            <FilterControl
                              key={`filter-part-${part.field}`}
                              column={part}
                              value={filters[part.field]}
                              onChange={onFilterChange}
                              options={filterOptions[part.field]}
                            />
                          ))}
                        </div>
                      ) : null}
                    </th>
                  );
                })}
                {showActions && <th className="border-b p-1" />}
              </tr>
            )}
          </thead>
          <tbody>
            {displayRows.map((row, index) => {
              const id = rowKey(row) || `row-${index}`;
              return (
                <tr
                  key={id}
                  onClick={onOpen ? () => openRow(row) : undefined}
                  onKeyDown={onOpen ? (event) => onRowKeyDown(event, row) : undefined}
                  className={`border-t ${onOpen ? "cursor-pointer hover:bg-gray-50" : ""}`}
                  role={onOpen ? "link" : undefined}
                  tabIndex={onOpen ? 0 : undefined}
                  aria-label={onOpen ? openLabel(row) : undefined}
                  data-testid={`row-${rowKey(row)}`}
                >
                  {showSelection && (
                    <td className="p-2" onClick={(event) => event.stopPropagation()}>
                      <SelectionCheckbox
                        checked={selection.has(rowKey(row))}
                        onChange={() => selection.toggle(rowKey(row))}
                        label={selectionLabel(row)}
                      />
                    </td>
                  )}
                  {cols.map((column) => (
                    <td
                      key={`td-${column.field}`}
                      className={`p-2 align-top ${column.cellClassName || ""}`}
                    >
                      {cellValue(row, column)}
                    </td>
                  ))}
                  {showActions && (
                    <td className="p-2" onClick={(event) => event.stopPropagation()}>
                      <div className="flex justify-end gap-2">{rowActions(row, { mobile: false })}</div>
                    </td>
                  )}
                </tr>
              );
            })}
          </tbody>
        </table>
      </DesktopTableWrap>

      <div className="text-xs text-gray-500 mt-2">
        Mostrando {displayRows.length}
        {Number.isFinite(totalCount) ? ` de ${totalCount}` : ""}
        {countLabel ? ` ${countLabel}` : ""}.
      </div>
    </div>
  );
}
