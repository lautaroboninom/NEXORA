// web/src/hooks/useQueueBadges.js
//
// Glue de React para el indicador de novedades del menú. La lógica de qué es
// "nuevo" vive en `lib/queue-badges.js`; acá sólo está el poll y el aviso entre
// el sidebar y la pantalla que el usuario abre.
import { useCallback, useEffect, useState } from "react";
import { useAuth } from "../context/AuthContext";
import { getColasNovedades } from "../lib/api";
import { markQueueSeen, queueBadgeCounts } from "../lib/queue-badges";

// Mismo ritmo que la campana de notificaciones: la consulta es de ids sueltos.
const REFRESH_MS = 60000;

// La última foto que devolvió el servidor, compartida por todos los que la
// miran: el sidebar la consulta en loop y la pantalla que se abre la usa para
// marcar la cola como vista, sin pedirla dos veces.
let ultimaFoto = {};
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

async function refrescarFoto() {
  const data = await getColasNovedades();
  ultimaFoto = (data && data.colas) || {};
  notificar();
  return ultimaFoto;
}

/** Contadores por cola, listos para dibujar. `{}` mientras no haya datos. */
export function useQueueBadges({ enabled = true } = {}) {
  const { user } = useAuth();
  const userId = user?.id;
  const [counts, setCounts] = useState({});
  const recalcular = useCallback(
    () => setCounts(queueBadgeCounts(ultimaFoto, userId)),
    [userId]
  );

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
      refrescarFoto().catch(() => {});
    };
    tick();
    const timer = window.setInterval(tick, REFRESH_MS);
    return () => {
      suscriptores.delete(recalcular);
      window.clearInterval(timer);
    };
  }, [enabled, recalcular]);

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
export function useMarkQueueSeen(queueKey) {
  const { user } = useAuth();
  const userId = user?.id;
  useEffect(() => {
    if (!queueKey) return undefined;
    let cancelado = false;
    (async () => {
      try {
        const foto = await refrescarFoto();
        if (cancelado) return;
        if (markQueueSeen(foto, queueKey, userId)) notificar();
      } catch (_) {
        // Sin foto no se marca nada: el número queda hasta la próxima visita.
      }
    })();
    return () => {
      cancelado = true;
    };
  }, [queueKey, userId]);
}
