import { useMemo } from "react";

import { QUEUE_NEW_ROW_CLASS } from "../lib/queue-badges";

/** Cada fila usa el reloj de la visita, incluso si aparece después por un filtro. */
export default function QueueHighlightRow({
  as: Component = "tr",
  highlight,
  ingresoId,
  className = "",
  style,
  ...props
}) {
  const startedAt = highlight?.startedAt;
  // El retraso queda fijo mientras esta fila esté montada: ordenar o renderizar
  // otra vez no reinicia ni adelanta su animación.
  const delay = useMemo(() => startedAt == null ? 0 : Math.min(0, startedAt - Date.now()), [startedAt]);
  const active = startedAt != null && highlight.ids.has(Number(ingresoId));
  return (
    <Component
      {...props}
      className={`${className}${active ? ` ${QUEUE_NEW_ROW_CLASS}` : ""}`}
      style={active ? { ...style, animationDelay: `${delay}ms` } : style}
    />
  );
}
