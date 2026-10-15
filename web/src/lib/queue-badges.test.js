import test from "node:test";
import assert from "node:assert/strict";
import {
  badgeLabel,
  countNew,
  firstVisibleNewRow,
  isRowInView,
  newQueueIds,
  markQueueSeen,
  queueBadgeCounts,
  QUEUE_BY_PATH,
  QUEUE_KEYS,
  QUEUE_NEW_ROW_CLASS,
  readSeenIds,
  reconcileSeenIds,
  seenStorageKey,
  writeSeenIds,
} from "./queue-badges.js";

// localStorage de mentira: node no tiene uno y el módulo lo recibe por parámetro.
const fakeStore = (inicial = {}) => {
  const datos = new Map(Object.entries(inicial));
  return {
    getItem: (k) => (datos.has(k) ? datos.get(k) : null),
    setItem: (k, v) => datos.set(k, String(v)),
    datos,
  };
};

test("las filas resaltadas coinciden con el contador antes de marcar como vista", () => {
  const store = fakeStore();
  const key = QUEUE_KEYS.PENDIENTES_GENERAL;
  writeSeenIds(3, key, [1, 2], store);
  const colas = { [key]: [2, "3", 4] };
  const ids = newQueueIds(readSeenIds(3, key, store), colas[key]);
  assert.deepEqual([...ids], [3, 4]);
  assert.equal(ids.size, queueBadgeCounts(colas, 3, store)[key]);
  markQueueSeen(colas, key, 3, store);
  assert.deepEqual([...ids], [3, 4], "la visita conserva las novedades capturadas");
  assert.equal(newQueueIds(readSeenIds(3, key, store), colas[key]).size, 0);
});

test("el resaltado respeta usuario, cola y ausencia de historial", () => {
  const store = fakeStore();
  writeSeenIds(3, QUEUE_KEYS.MIS_PENDIENTES, [1], store);
  assert.deepEqual([...newQueueIds(readSeenIds(3, QUEUE_KEYS.MIS_PENDIENTES, store), [1, 2])], [2]);
  assert.equal(newQueueIds(readSeenIds(9, QUEUE_KEYS.MIS_PENDIENTES, store), [1, 2]).size, 0);
  assert.equal(newQueueIds(readSeenIds(3, QUEUE_KEYS.PENDIENTES_GENERAL, store), [1, 2]).size, 0);
  assert.equal(newQueueIds(null, [1, 2]).size, 0);
});

// Un elemento del DOM reducido a lo que mira el salto: si tiene caja en la página.
const fakeRow = (nombre, visible) => ({ nombre, getClientRects: () => (visible ? [{}] : []) });

test("el salto va a la primera fila nueva que se ve, no a la copia oculta", () => {
  const consultas = [];
  // La tarjeta de celular va antes en el DOM y en escritorio está oculta.
  const root = {
    querySelectorAll: (selector) => {
      consultas.push(selector);
      return [fakeRow("tarjeta 29840", false), fakeRow("renglón 29840", true), fakeRow("renglón 29847", true)];
    },
  };
  assert.equal(firstVisibleNewRow(root).nombre, "renglón 29840");
  assert.deepEqual(consultas, [`.${QUEUE_NEW_ROW_CLASS}`]);
});

test("sin filas nuevas visibles no hay salto", () => {
  assert.equal(firstVisibleNewRow({ querySelectorAll: () => [fakeRow("tarjeta", false)] }), null);
  assert.equal(firstVisibleNewRow({ querySelectorAll: () => [] }), null);
  assert.equal(firstVisibleNewRow(null), null);
});

test("una fila que ya se ve entera no mueve la pantalla", () => {
  assert.equal(isRowInView({ top: 100, bottom: 140 }, 800), true);
  assert.equal(isRowInView({ top: 3200, bottom: 3240 }, 800), false, "al final de la lista");
  assert.equal(isRowInView({ top: 780, bottom: 820 }, 800), false, "cortada abajo");
  assert.equal(isRowInView({ top: -20, bottom: 20 }, 800), false, "cortada arriba");
});

test("la primera visita no muestra número", () => {
  // Sin foto previa no hay con qué comparar: mostrar el total sería ruido.
  assert.equal(countNew(null, [1, 2, 3]), 0);
  assert.equal(countNew(undefined, [1, 2, 3]), 0);
});

test("cuenta sólo lo que no estaba la última vez", () => {
  assert.equal(countNew(new Set([1, 2]), [1, 2, 3]), 1);
  assert.equal(countNew(new Set([1, 2]), [3, 4]), 2);
  assert.equal(countNew(new Set([1, 2, 3]), [1, 2]), 0, "que se vayan órdenes no es novedad");
  assert.equal(countNew(new Set([1, 2]), []), 0);
});

test("una orden que sale de la cola y vuelve cuenta de nuevo", () => {
  const store = fakeStore();
  markQueueSeen({ mis_pendientes: [7, 8] }, QUEUE_KEYS.MIS_PENDIENTES, 3, store);
  // La 7 se va (la reparó): al mirar de nuevo, la foto queda sin la 7.
  markQueueSeen({ mis_pendientes: [8] }, QUEUE_KEYS.MIS_PENDIENTES, 3, store);
  const counts = queueBadgeCounts({ mis_pendientes: [7, 8] }, 3, store);
  assert.equal(counts.mis_pendientes, 1);
});

test("la cola que nunca se abrió en este navegador se siembra sola y después avisa", () => {
  // El caso del reporte: nadie abrió "Listos para liberar" en esa PC, entra un
  // equipo y el menú no mostraba nada porque no había foto contra qué comparar.
  const store = fakeStore();
  const antes = { listos_para_liberar: [1, 2] };
  reconcileSeenIds(antes, 3, store);
  assert.equal(queueBadgeCounts(antes, 3, store).listos_para_liberar, 0);

  const despues = { listos_para_liberar: [1, 2, 5] };
  reconcileSeenIds(despues, 3, store);
  assert.equal(queueBadgeCounts(despues, 3, store).listos_para_liberar, 1);
});

test("una orden que se va y vuelve sin que nadie abra la pantalla cuenta de nuevo", () => {
  const store = fakeStore();
  markQueueSeen({ listos_para_liberar: [7, 8] }, QUEUE_KEYS.LISTOS_PARA_LIBERAR, 3, store);
  // Se emite el presupuesto de la 7 y sale de la cola; el menú lo ve en su consulta.
  reconcileSeenIds({ listos_para_liberar: [8] }, 3, store);
  // Lo aprueban y vuelve: es novedad aunque ya se hubiera visto antes.
  const colas = { listos_para_liberar: [7, 8] };
  reconcileSeenIds(colas, 3, store);
  assert.equal(queueBadgeCounts(colas, 3, store).listos_para_liberar, 1);
});

test("poner al día la foto no apaga una novedad ni toca las colas que no vinieron", () => {
  const store = fakeStore();
  markQueueSeen({ listos_para_liberar: [1] }, QUEUE_KEYS.LISTOS_PARA_LIBERAR, 3, store);
  writeSeenIds(3, QUEUE_KEYS.PENDIENTES_PRESUPUESTO, [40, 41], store);
  const colas = { listos_para_liberar: [1, 2] };
  reconcileSeenIds(colas, 3, store);
  reconcileSeenIds(colas, 3, store);
  assert.equal(queueBadgeCounts(colas, 3, store).listos_para_liberar, 1);
  assert.deepEqual([...readSeenIds(3, QUEUE_KEYS.PENDIENTES_PRESUPUESTO, store)], [40, 41]);
});

test("la foto es por usuario: dos logins en la misma máquina no se pisan", () => {
  const store = fakeStore();
  writeSeenIds(3, QUEUE_KEYS.PENDIENTES_GENERAL, [1, 2], store);
  assert.deepEqual([...readSeenIds(3, QUEUE_KEYS.PENDIENTES_GENERAL, store)], [1, 2]);
  assert.equal(readSeenIds(9, QUEUE_KEYS.PENDIENTES_GENERAL, store), null);
  assert.notEqual(
    seenStorageKey(3, QUEUE_KEYS.PENDIENTES_GENERAL),
    seenStorageKey(9, QUEUE_KEYS.PENDIENTES_GENERAL),
  );
});

test("una cola sin permiso no viene en la respuesta y no genera contador", () => {
  const store = fakeStore();
  writeSeenIds(3, QUEUE_KEYS.PENDIENTES_PRESUPUESTO, [1], store);
  const counts = queueBadgeCounts({ pendientes_general: [1, 2] }, 3, store);
  assert.deepEqual(Object.keys(counts), ["pendientes_general"]);
});

test("storage roto no rompe el menú", () => {
  const roto = {
    getItem: () => {
      throw new Error("bloqueado");
    },
    setItem: () => {
      throw new Error("bloqueado");
    },
  };
  assert.equal(readSeenIds(3, QUEUE_KEYS.MIS_PENDIENTES, roto), null);
  assert.doesNotThrow(() => writeSeenIds(3, QUEUE_KEYS.MIS_PENDIENTES, [1], roto));
  assert.doesNotThrow(() => reconcileSeenIds({ mis_pendientes: [1, 2] }, 3, roto));
  assert.deepEqual(queueBadgeCounts({ mis_pendientes: [1, 2] }, 3, roto), { mis_pendientes: 0 });
});

test("el número se corta en 99+", () => {
  assert.equal(badgeLabel(0), "");
  assert.equal(badgeLabel(1), "1");
  assert.equal(badgeLabel(99), "99");
  assert.equal(badgeLabel(140), "99+");
});

test("las cuatro colas del menú están mapeadas por ruta", () => {
  assert.deepEqual(QUEUE_BY_PATH, {
    "/pendientes": "pendientes_general",
    "/tecnico": "mis_pendientes",
    "/pendientes-presupuesto": "pendientes_presupuesto",
    "/listos-para-liberar": "listos_para_liberar",
  });
});
