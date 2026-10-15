// web/src/hooks/useQueueBadges.js
//
// Glue de React para el indicador de novedades del menú. La lógica de qué es
// "nuevo" vive en `lib/queue-badges.js`; acá sólo está el poll y el aviso entre
// el sidebar y la pantalla que el usuario abre.
import { useCallback, useEffect, useState } from "react";
import { useAuth } from "../context/AuthContext";
import { getColasNovedades } from "../lib/api";
import {
  firstVisibleNewRow,
  isRowInView,
  markQueueSeen,
  newQueueIds,
  readSeenIds,
  queueBadgeCounts,
  reconcileSeenIds,
} from "../lib/queue-badges";

export const QUEUE_HIGHLIGHT_MS = 10000;

// Mismo ritmo que la campana de notificaciones: la consulta es de ids sueltos.
const REFRESH_MS = 60000;

// La última foto que devolvió el servidor, compartida por todos los que la
// miran: el sidebar la consulta en loop y la pantalla que se abre la usa para
// marcar la cola como vista, sin pedirla dos veces.
let ultimaFoto = {};
// De quién es la foto. La respuesta es por sesión: si se cambia de usuario sin
// recargar, la foto del login anterior no sirve ni para contar ni para escribir
// en la foto guardada del nuevo.
let ultimaFotoDe = null;
// Cada pedido lleva número y una respuesta más vieja que la última aplicada se
// descarta. Si no, una foto atrasada "olvidaba" de la foto guardada una orden que
// el usuario acababa de ver, y la volvía a contar como novedad.
let ultimoPedido = 0;
let ultimoAplicado = 0;
const suscriptores = new Set();

function notificar() {
  for (const avisar of suscriptores) {
    try {
      avisar();
    } catch (_) {
      // Un suscriptor desmontado no puede tumbar al resto.
    }
  }
}

async function refrescarFoto(userId) {
  const pedido = ++ultimoPedido;
  const data = await getColasNovedades();
  if (pedido < ultimoAplicado) return ultimaFoto;
  ultimoAplicado = pedido;
  ultimaFoto = (data && data.colas) || {};
  ultimaFotoDe = userId;
  notificar();
  return ultimaFoto;
}

/** Contadores por cola, listos para dibujar. `{}` mientras no haya datos. */
export function useQueueBadges({ enabled = true } = {}) {
  const { user } = useAuth();
  const userId = user?.id;
  const [counts, setCounts] = useState({});
  const recalcular = useCallback(() => {
    const foto = ultimaFotoDe === userId ? ultimaFoto : {};
    // Primero se pone al día la foto guardada y después se cuenta contra ella.
    reconcileSeenIds(foto, userId);
    setCounts(queueBadgeCounts(foto, userId));
  }, [userId]);

  useEffect(() => {
    if (!enabled) {
      setCounts({});
      return undefined;
    }
    suscriptores.add(recalcular);
    recalcular();
    // El indicador es accesorio: si la consulta falla se queda como está y no
    // se le muestra ningún error al usuario.
    const tick = () => {
      refrescarFoto(userId).catch(() => {});
    };
    tick();
    const timer = window.setInterval(tick, REFRESH_MS);
    return () => {
      suscriptores.delete(recalcular);
      window.clearInterval(timer);
    };
  }, [enabled, recalcular, userId]);

  return counts;
}

/**
 * Apaga el indicador de una cola: la pantalla que la muestra la marca como
 * vista al abrirse.
 *
 * Marca con la foto del servidor y no con las filas de la pantalla a propósito:
 * varios listados filtran (por técnico, por texto) y si marcáramos lo filtrado,
 * lo que quedó fuera del filtro seguiría contando como novedad para siempre.
 */
export function useMarkQueueSeen(queueKey, { ready = true } = {}) {
  const { user } = useAuth();
  const userId = user?.id;
  const [visit, setVisit] = useState(null);
  useEffect(() => {
    setVisit(null);
    if (!queueKey || userId == null) return undefined;
    let cancelado = false;
    (async () => {
      try {
        const foto = await refrescarFoto(userId);
        if (cancelado || ultimaFotoDe !== userId) return;
        // Capturar antes de escribir la nueva foto: después la diferencia sería cero.
        const ids = newQueueIds(readSeenIds(userId, queueKey), foto[queueKey]);
        setVisit({ userId, queueKey, ids, startedAt: null });
        if (markQueueSeen(foto, queueKey, userId)) notificar();
      } catch (_) {
        // Sin foto no se marca nada: el número queda hasta la próxima visita.
      }
    })();
    return () => {
      cancelado = true;
    };
  }, [queueKey, userId]);

  const currentVisit = visit?.userId === userId && visit?.queueKey === queueKey ? visit : null;
  useEffect(() => {
    if (!ready || !currentVisit?.ids.size || currentVisit.startedAt !== null) return;
    setVisit((previous) => previous === currentVisit ? { ...previous, startedAt: Date.now() } : previous);
  }, [ready, currentVisit]);

  const startedAt = currentVisit?.startedAt;
  useEffect(() => {
    if (startedAt == null) return undefined;
    const timer = window.setTimeout(() => {
      setVisit((previous) => previous?.startedAt === startedAt
        ? { ...previous, ids: new Set() }
        : previous);
    }, Math.max(0, QUEUE_HIGHLIGHT_MS - (Date.now() - startedAt)));
    return () => window.clearTimeout(timer);
  }, [startedAt]);

  return startedAt == null ? null : currentVisit;
}

/**
 * Al abrir una cola con novedades, lleva la pantalla hasta la primera.
 *
 * Las colas van de la orden más vieja a la más nueva, así que lo que entró queda
 * al final de la lista, fuera de la vista. Se mueve una sola vez por visita, en el
 * orden que se está mostrando, y no se mueve si la fila ya se ve.
 */
export function useScrollToFirstNewRow(containerRef, highlight) {
  const startedAt = highlight?.startedAt;
  useEffect(() => {
    if (startedAt == null) return undefined;
    // Dos cuadros de espera: que la lista termine de dibujarse y que Pendientes de
    // presupuesto restaure antes su scroll guardado, que si no pisaría el salto.
    let frame = window.requestAnimationFrame(() => {
      frame = window.requestAnimationFrame(() => {
        const row = firstVisibleNewRow(containerRef.current);
        if (!row || isRowInView(row.getBoundingClientRect(), window.innerHeight)) return;
        const reduceMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches;
        row.scrollIntoView({ block: "center", behavior: reduceMotion ? "auto" : "smooth" });
      });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [containerRef, startedAt]);
}
