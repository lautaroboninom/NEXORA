const textCollator = new Intl.Collator("es-AR", {
  numeric: true,
  sensitivity: "base",
});

export function parseSort(sort) {
  const raw = String(sort || "").trim();
  if (!raw) return { field: "", direction: "asc" };
  return raw.startsWith("-")
    ? { field: raw.slice(1), direction: "desc" }
    : { field: raw, direction: "asc" };
}

export function nextSort(sort, field) {
  const current = String(sort || "").trim();
  const normalizedField = String(field || "").trim();
  if (!normalizedField) return "";
  if (current === normalizedField) return `-${normalizedField}`;
  if (current === `-${normalizedField}`) return "";
  return normalizedField;
}

export function sortForDirection(field, direction) {
  const normalizedField = String(field || "").trim();
  if (!normalizedField) return "";
  return direction === "desc" ? `-${normalizedField}` : normalizedField;
}

function isEmpty(value) {
  return value == null || (typeof value === "string" && value.trim() === "");
}

function numberValue(value) {
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  const normalized = String(value ?? "")
    .trim()
    .replace(/\s/g, "")
    .replace(/\.(?=\d{3}(?:\D|$))/g, "")
    .replace(",", ".");
  if (!normalized) return null;
  const parsed = Number(normalized);
  return Number.isFinite(parsed) ? parsed : null;
}

function dateValue(value) {
  if (value instanceof Date) return value.getTime();
  const parsed = new Date(value).getTime();
  return Number.isNaN(parsed) ? null : parsed;
}

function compareNonEmpty(a, b, definition = {}) {
  if (typeof definition.compare === "function") {
    return definition.compare(a, b);
  }
  if (definition.type === "number") {
    const av = numberValue(a);
    const bv = numberValue(b);
    if (av == null && bv == null) return 0;
    if (av == null) return 1;
    if (bv == null) return -1;
    return av - bv;
  }
  if (definition.type === "date") {
    const av = dateValue(a);
    const bv = dateValue(b);
    if (av == null && bv == null) return 0;
    if (av == null) return 1;
    if (bv == null) return -1;
    return av - bv;
  }
  if (definition.type === "boolean") {
    return Number(Boolean(a)) - Number(Boolean(b));
  }
  return textCollator.compare(String(a), String(b));
}

export function compareTableValues(a, b, definition = {}, direction = "asc") {
  const aEmpty = isEmpty(a);
  const bEmpty = isEmpty(b);
  if (aEmpty && bEmpty) return 0;
  if (aEmpty) return 1;
  if (bEmpty) return -1;
  const compared = compareNonEmpty(a, b, definition);
  return direction === "desc" ? -compared : compared;
}

export function sortRows(rows, sort, columns = {}) {
  const data = Array.isArray(rows) ? rows : [];
  const { field, direction } = parseSort(sort);
  const definition = columns?.[field];
  if (!field || !definition) return data;
  const accessor =
    typeof definition === "function"
      ? definition
      : typeof definition.accessor === "function"
        ? definition.accessor
        : typeof definition.getValue === "function"
          ? definition.getValue
          : (row) => row?.[field];
  const normalizedDefinition = typeof definition === "function" ? {} : definition;

  return data
    .map((row, index) => ({ row, index }))
    .sort((left, right) => {
      const compared = compareTableValues(
        accessor(left.row),
        accessor(right.row),
        normalizedDefinition,
        direction
      );
      return compared || left.index - right.index;
    })
    .map(({ row }) => row);
}
