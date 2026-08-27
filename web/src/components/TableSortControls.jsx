import { ArrowDown, ArrowUp, ArrowUpDown } from "lucide-react";
import { nextSort, parseSort, sortForDirection } from "../lib/table-sort";

/**
 * Botón de orden suelto, para las celdas que apilan más de un criterio y no
 * pueden anidar otro `<th>` adentro.
 */
export function SortButton({ field, sort, onSortChange, children, label, title }) {
  const content = children ?? label;
  if (typeof onSortChange !== "function") {
    return <span>{content}</span>;
  }

  const parsed = parseSort(sort);
  const active = parsed.field === field;
  const Icon = !active ? ArrowUpDown : parsed.direction === "desc" ? ArrowDown : ArrowUp;
  const description = typeof content === "string" ? content.toLowerCase() : field;

  return (
    <button
      type="button"
      onClick={() => onSortChange(nextSort(sort, field))}
      className="inline-flex w-full items-center gap-1 text-left hover:text-gray-900 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500"
      aria-label={`Ordenar por ${description}`}
      title={title || `Ordenar por ${description}`}
    >
      <span>{content}</span>
      <Icon
        aria-hidden="true"
        className={`h-3.5 w-3.5 shrink-0 ${active ? "text-blue-700" : "text-gray-400"}`}
      />
    </button>
  );
}

export function SortableTh({
  field,
  sort,
  onSortChange,
  children,
  label,
  className = "",
  title,
  colSpan,
}) {
  const content = children ?? label;
  if (typeof onSortChange !== "function") {
    return (
      <th scope="col" className={className} title={title} colSpan={colSpan}>
        {content}
      </th>
    );
  }
  const parsed = parseSort(sort);
  const active = parsed.field === field;
  const ariaSort = active ? (parsed.direction === "desc" ? "descending" : "ascending") : "none";

  return (
    <th
      scope="col"
      aria-sort={ariaSort}
      className={className}
      title={title}
      colSpan={colSpan}
    >
      <SortButton field={field} sort={sort} onSortChange={onSortChange} title={title}>
        {content}
      </SortButton>
    </th>
  );
}

export function MobileSortControl({
  sort,
  onSortChange,
  options,
  label = "Ordenar por",
  className = "",
  mobileOnly = true,
}) {
  const parsed = parseSort(sort);
  const field = parsed.field;

  return (
    <div className={`flex flex-wrap items-end gap-2 ${mobileOnly ? "md:hidden" : ""} ${className}`}>
      <label className="min-w-48 flex-1 text-xs font-medium text-gray-600">
        {label}
        <select
          value={field}
          onChange={(event) => onSortChange?.(event.target.value)}
          className="mt-1 w-full rounded border border-gray-300 bg-white p-2 text-sm text-gray-900"
        >
          <option value="">Orden predeterminado</option>
          {(options || []).map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </label>
      <button
        type="button"
        className="btn min-h-10"
        disabled={!field}
        onClick={() =>
          onSortChange?.(sortForDirection(field, parsed.direction === "desc" ? "asc" : "desc"))
        }
        aria-label={parsed.direction === "desc" ? "Cambiar a orden ascendente" : "Cambiar a orden descendente"}
        title={parsed.direction === "desc" ? "Orden descendente" : "Orden ascendente"}
      >
        {parsed.direction === "desc" ? (
          <ArrowDown aria-hidden="true" className="h-4 w-4" />
        ) : (
          <ArrowUp aria-hidden="true" className="h-4 w-4" />
        )}
        {parsed.direction === "desc" ? "Descendente" : "Ascendente"}
      </button>
    </div>
  );
}
