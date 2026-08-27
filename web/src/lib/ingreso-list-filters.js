import { norm } from "./ui-helpers.js";
import { INGRESO_LIST_SORT_COLUMNS } from "./ingreso-list-sort.js";

/** Valor de filtro que busca las filas que tienen esa columna vacía. */
export const EMPTY_FILTER = "__none__";

/**
 * De dónde sale el valor de una columna para filtrar.
 *
 * Por defecto reusa el accessor que la columna ya declara para ordenar, así una
 * columna se define una sola vez y sirve para las dos cosas.
 */
export function columnFilterValue(row, column, sortColumns = INGRESO_LIST_SORT_COLUMNS) {
  if (!column) return "";
  if (typeof column.filterValue === "function") return column.filterValue(row);

  const definition = sortColumns?.[column.field];
  if (typeof definition === "function") return definition(row);
  if (typeof definition?.accessor === "function") return definition.accessor(row);
  if (typeof definition?.getValue === "function") return definition.getValue(row);
  return row?.[column.field];
}

function filterableColumns(columns) {
  return (columns || []).filter((column) => column?.filter && column?.field);
}

function matchesText(value, needle) {
  return norm(value).includes(norm(needle));
}

function matchesSelect(value, selected) {
  const raw = String(value ?? "").trim();
  if (selected === EMPTY_FILTER) return !raw;
  return norm(raw) === norm(selected);
}

/** ¿La fila pasa todos los filtros por columna que están cargados? */
export function matchesColumnFilters(
  row,
  columns,
  filters = {},
  sortColumns = INGRESO_LIST_SORT_COLUMNS
) {
  for (const column of filterableColumns(columns)) {
    const selected = filters?.[column.field];
    if (!selected) continue;
    const value = columnFilterValue(row, column, sortColumns);
    if (column.filter === "select") {
      if (!matchesSelect(value, selected)) return false;
    } else if (!matchesText(value, selected)) {
      return false;
    }
  }
  return true;
}

function optionLabel(column, value) {
  if (typeof column.filterLabel === "function") {
    const label = column.filterLabel(value);
    if (label) return String(label);
  }
  return String(value);
}

/**
 * Opciones de cada `<select>`, sacadas de los datos que hay en pantalla.
 *
 * Se ofrece "(sin dato)" sólo cuando alguna fila realmente tiene la columna
 * vacía, para no llenar los combos de opciones que no filtran nada.
 */
export function columnFilterOptions(rows, columns, sortColumns = INGRESO_LIST_SORT_COLUMNS) {
  const options = {};
  for (const column of filterableColumns(columns)) {
    if (column.filter !== "select") continue;
    const seen = new Map();
    let hasEmpty = false;
    for (const row of rows || []) {
      const raw = columnFilterValue(row, column, sortColumns);
      const value = String(raw ?? "").trim();
      if (!value) {
        hasEmpty = true;
        continue;
      }
      if (!seen.has(value)) seen.set(value, optionLabel(column, value));
    }
    const list = Array.from(seen, ([value, label]) => ({ value, label })).sort((a, b) =>
      a.label.localeCompare(b.label, "es-AR", { numeric: true, sensitivity: "base" })
    );
    if (hasEmpty) list.push({ value: EMPTY_FILTER, label: "(sin dato)" });
    options[column.field] = list;
  }
  return options;
}

export function activeFilterCount(filters = {}) {
  return Object.values(filters || {}).filter((value) => String(value ?? "").trim() !== "").length;
}

/** Filtros por columna serializados en un solo parámetro de URL. */
export function serializeColumnFilters(filters = {}) {
  const parts = [];
  for (const [field, value] of Object.entries(filters || {})) {
    const raw = String(value ?? "").trim();
    if (!raw) continue;
    parts.push(`${encodeURIComponent(field)}:${encodeURIComponent(raw)}`);
  }
  return parts.join(";");
}

export function parseColumnFilters(raw) {
  const filters = {};
  for (const part of String(raw || "").split(";")) {
    if (!part) continue;
    const separator = part.indexOf(":");
    if (separator <= 0) continue;
    try {
      const field = decodeURIComponent(part.slice(0, separator));
      const value = decodeURIComponent(part.slice(separator + 1));
      if (field && value) filters[field] = value;
    } catch (_) {
      // Un parámetro roto en la URL no debería tirar abajo la página.
    }
  }
  return filters;
}
