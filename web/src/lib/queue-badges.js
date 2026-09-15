// web/src/lib/queue-badges.js
//
// Indicador de novedades en las colas del menú: cuántas órdenes entraron a una
// cola desde la última vez que el usuario abrió esa pestaña.
//
// El "ya lo vi" se guarda **en este navegador**, por usuario y por cola. No hay
// nada de esto en el servidor: el backend sólo contesta qué órdenes hay ahora en
// cada cola (`/api/colas/novedades/`) y la comparación se hace acá.

export const QUEUE_KEYS = {
  PENDIENTES_GENERAL: "pendientes_general",
  MIS_PENDIENTES: "mis_pendientes",
  PENDIENTES_PRESUPUESTO: "pendientes_presupuesto",
  LISTOS_PARA_LIBERAR: "listos_para_liberar",
};

// La ruta es lo que el sidebar tiene a mano al pintar cada link.
export const QUEUE_BY_PATH = {
  "/pendientes": QUEUE_KEYS.PENDIENTES_GENERAL,
  "/tecnico": QUEUE_KEYS.MIS_PENDIENTES,
  "/pendientes-presupuesto": QUEUE_KEYS.PENDIENTES_PRESUPUESTO,
  "/listos-para-liberar": QUEUE_KEYS.LISTOS_PARA_LIBERAR,
};

// Arriba de esto el número deja de ser información y pasa a ser ruido.
export const MAX_BADGE = 99;

function storage() {
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch (_) {
    return null;
  }
}

export const seenStorageKey = (userId, queueKey) =>
  `nexora:cola-vista:${userId ?? "anon"}:${queueKey}`;

/**
 * Los ids que el usuario ya vio en esa cola, o `null` si nunca la abrió en este
 * navegador. La diferencia importa: `null` no es "no vio nada" sino "todavía no
 * hay contra qué comparar", y ahí no se muestra número (ver `countNew`).
 */
export function readSeenIds(userId, queueKey, store = storage()) {
  if (!store) return null;
  try {
    const raw = store.getItem(seenStorageKey(userId, queueKey));
    if (raw == null) return null;
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return null;
    return new Set(parsed.map((id) => Number(id)).filter((id) => Number.isFinite(id)));
  } catch (_) {
    return null;
  }
}

/**
 * Guarda la foto de la cola en el momento en que el usuario la miró. Se pisa
 * entera, no se acumula: si una orden sale de la cola y más tarde vuelve, tiene
 * que volver a contar como novedad.
 */
export function writeSeenIds(userId, queueKey, ids, store = storage()) {
  if (!store) return;
  try {
    const limpios = (Array.isArray(ids) ? ids : [])
      .map((id) => Number(id))
      .filter((id) => Number.isFinite(id));
    store.setItem(seenStorageKey(userId, queueKey), JSON.stringify(limpios));
  } catch (_) {
    // Navegador con storage bloqueado: se pierde el indicador, no la navegación.
  }
}

/**
 * Cuántos de los ids actuales no estaban la última vez.
 *
 * La primera vez (`seen === null`) devuelve 0 a propósito: si no, el técnico
 * abriría NEXORA y se encontraría con un "47" al lado de cada pestaña, que no
 * avisa nada. La cola se siembra en silencio y a partir de ahí sí avisa.
 */
export function countNew(seen, currentIds) {
  if (!(seen instanceof Set)) return 0;
  const actuales = Array.isArray(currentIds) ? currentIds : [];
  let nuevas = 0;
  for (const raw of actuales) {
    const id = Number(raw);
    if (Number.isFinite(id) && !seen.has(id)) nuevas += 1;
  }
  return nuevas;
}

/** El número tal cual se dibuja: "99+" para no romper el ancho del menú. */
export function badgeLabel(count) {
  if (!Number.isFinite(count) || count <= 0) return "";
  return count > MAX_BADGE ? `${MAX_BADGE}+` : String(count);
}

/**
 * Contadores de todas las colas que vinieron en la respuesta. Una cola que el
 * usuario no tiene permiso de ver no viene en `colas` y por lo tanto tampoco
 * aparece acá: el sidebar no dibuja indicador para ella.
 */
export function queueBadgeCounts(colas, userId, store = storage()) {
  const salida = {};
  for (const [queueKey, ids] of Object.entries(colas || {})) {
    salida[queueKey] = countNew(readSeenIds(userId, queueKey, store), ids);
  }
  return salida;
}

/**
 * Pone al día la foto guardada con lo que contestó el servidor, sin que el usuario
 * abra la pantalla. El menú lo corre con cada consulta, y hace dos cosas para que el
 * número no se quede mudo:
 *
 * - Siembra la cola que nunca se abrió en este navegador. Antes la foto sólo nacía al
 *   entrar a la pantalla: quien no la abría nunca en esa PC no veía un número jamás.
 * - Olvida las órdenes que ya no están. Si no, una OS que se fue de la cola y volvió
 *   sin que nadie abriera la pantalla en el medio (reparada → presupuesto emitido →
 *   aprobado) seguía figurando como vista y no contaba como novedad.
 *
 * Nunca suma a la foto una orden que no estaba: eso lo hace sólo abrir la pantalla
 * (`markQueueSeen`), que es lo que apaga el número.
 */
export function reconcileSeenIds(colas, userId, store = storage()) {
  for (const [queueKey, ids] of Object.entries(colas || {})) {
    if (!Array.isArray(ids)) continue;
    const seen = readSeenIds(userId, queueKey, store);
    if (seen === null) {
      writeSeenIds(userId, queueKey, ids, store);
      continue;
    }
    const actuales = new Set(ids.map((id) => Number(id)));
    const vigentes = [...seen].filter((id) => actuales.has(id));
    if (vigentes.length !== seen.size) writeSeenIds(userId, queueKey, vigentes, store);
  }
}

/** Marca la cola como vista con la foto que devolvió el servidor recién. */
export function markQueueSeen(colas, queueKey, userId, store = storage()) {
  const ids = (colas || {})[queueKey];
  if (!Array.isArray(ids)) return false;
  writeSeenIds(userId, queueKey, ids, store);
  return true;
}
