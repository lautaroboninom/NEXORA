import { useCallback, useMemo, useState } from "react";
import { ingresoIdOf } from "../lib/ui-helpers";

/**
 * Selección de filas por id, con "seleccionar todos los visibles".
 *
 * La selección sobrevive al filtro de texto a propósito: se puede elegir unas
 * cuantas, buscar otro cliente y seguir sumando. Por eso `toggleAllVisible`
 * trabaja sólo sobre lo que está en pantalla y nunca pisa el resto.
 *
 * @param visibleIds ids de las filas que el filtro actual deja ver.
 */
export default function useRowSelection(visibleIds = []) {
  const [ids, setIds] = useState(() => new Set());

  const visible = useMemo(
    () => (visibleIds instanceof Set ? visibleIds : new Set(visibleIds || [])),
    [visibleIds]
  );

  const has = useCallback((id) => ids.has(id), [ids]);

  const toggle = useCallback((id) => {
    if (id == null || id === "") return;
    setIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const clear = useCallback(() => setIds(new Set()), []);

  const remove = useCallback((list) => {
    const gone = list instanceof Set ? list : new Set(list || []);
    if (gone.size === 0) return;
    setIds((prev) => {
      const next = new Set(prev);
      for (const id of gone) next.delete(id);
      return next;
    });
  }, []);

  const { allVisibleSelected, someVisibleSelected } = useMemo(() => {
    if (visible.size === 0) return { allVisibleSelected: false, someVisibleSelected: false };
    let selected = 0;
    for (const id of visible) if (ids.has(id)) selected += 1;
    return {
      allVisibleSelected: selected === visible.size,
      someVisibleSelected: selected > 0 && selected < visible.size,
    };
  }, [visible, ids]);

  const toggleAllVisible = useCallback(() => {
    setIds((prev) => {
      const next = new Set(prev);
      let selected = 0;
      for (const id of visible) if (next.has(id)) selected += 1;
      if (selected === visible.size) {
        for (const id of visible) next.delete(id);
      } else {
        for (const id of visible) next.add(id);
      }
      return next;
    });
  }, [visible]);

  /**
   * Las filas elegidas en el orden de la lista, no en el que se fueron
   * tildando. Importa: ese orden es el que termina teniendo el PDF del lote y
   * el Excel, y un comprobante con las órdenes salteadas no se entiende.
   */
  const selectedRowsIn = useCallback(
    (rows, keyFn = ingresoIdOf) =>
      (rows || []).filter((row) => {
        const id = keyFn(row);
        return id != null && ids.has(id);
      }),
    [ids]
  );

  return {
    ids,
    count: ids.size,
    has,
    toggle,
    toggleAllVisible,
    allVisibleSelected,
    someVisibleSelected,
    clear,
    remove,
    selectedRowsIn,
  };
}
