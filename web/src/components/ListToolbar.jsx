import { SlidersHorizontal } from "lucide-react";

/**
 * Barra de arriba de las listas: búsqueda, recarga, filtros y acciones masivas.
 *
 * Los filtros por columna van plegados detrás de un botón porque son muchos y
 * casi siempre se busca por texto; el contador deja ver que hay filtros puestos
 * aunque el panel esté cerrado.
 */
export default function ListToolbar({
  q,
  onQChange,
  searchPlaceholder = "Filtrar",
  searchLabel = "Filtrar lista",
  onReload,
  loading = false,
  showFilterToggle = false,
  filtersOpen = false,
  onToggleFilters,
  activeFilterCount = 0,
  onClearFilters,
  selectedCount = 0,
  children,
  className = "",
}) {
  return (
    <div className={`mb-3 flex flex-col gap-2 lg:flex-row lg:items-center ${className}`}>
      {typeof onQChange === "function" && (
        <input
          type="text"
          value={q || ""}
          onChange={(event) => onQChange(event.target.value)}
          placeholder={searchPlaceholder}
          className="border rounded p-2 w-full max-w-md"
          aria-label={searchLabel}
        />
      )}

      {typeof onReload === "function" && (
        <button
          className="btn"
          type="button"
          onClick={onReload}
          title="Recargar lista"
          disabled={loading}
          aria-busy={loading ? "true" : "false"}
        >
          Recargar
        </button>
      )}

      {showFilterToggle && (
        <button
          className="btn-secondary inline-flex items-center gap-2"
          type="button"
          onClick={onToggleFilters}
          aria-expanded={filtersOpen ? "true" : "false"}
          title="Mostrar u ocultar los filtros por columna"
        >
          <SlidersHorizontal className="h-4 w-4" aria-hidden="true" />
          Filtros avanzados
          {activeFilterCount > 0 ? ` (${activeFilterCount})` : ""}
        </button>
      )}

      {activeFilterCount > 0 && typeof onClearFilters === "function" && (
        <button
          className="text-sm text-blue-700 underline hover:text-blue-900"
          type="button"
          onClick={onClearFilters}
        >
          Limpiar filtros
        </button>
      )}

      {children ? (
        <div className="flex flex-wrap items-center gap-2 lg:ml-auto">
          {selectedCount > 0 && (
            <span className="text-sm text-gray-600">{selectedCount} elegidas</span>
          )}
          {children}
        </div>
      ) : null}
    </div>
  );
}
