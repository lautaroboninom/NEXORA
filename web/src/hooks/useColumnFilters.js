import { useCallback, useMemo } from "react";
import useQueryState from "./useQueryState";
import {
  activeFilterCount,
  parseColumnFilters,
  serializeColumnFilters,
} from "../lib/ingreso-list-filters";

/**
 * Filtros por columna guardados en un único parámetro `?f=` de la URL.
 *
 * Va todo junto en un parámetro para que agregar una columna filtrable no
 * obligue a sumar un hook más: los filtros son tantos y tan cambiantes por
 * página que una clave por columna se vuelve inmanejable.
 */
export default function useColumnFilters(key = "f") {
  // El filtrado usa el estado local, así que responde a cada tecla; lo que se
  // demora es sólo la escritura en la URL, que si no navega en cada letra.
  const [raw, setRaw] = useQueryState(key, "", { debounceMs: 250 });

  const filters = useMemo(() => parseColumnFilters(raw), [raw]);

  const setFilter = useCallback(
    (field, value) => {
      if (!field) return;
      setRaw((prev) => {
        const next = { ...parseColumnFilters(prev) };
        const clean = String(value ?? "").trim();
        if (clean) next[field] = clean;
        else delete next[field];
        return serializeColumnFilters(next);
      });
    },
    [setRaw]
  );

  const clearFilters = useCallback(() => setRaw(""), [setRaw]);

  return {
    filters,
    setFilter,
    clearFilters,
    activeCount: activeFilterCount(filters),
  };
}
