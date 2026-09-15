import test from "node:test";
import assert from "node:assert/strict";
import {
  badgeLabel,
  countNew,
  markQueueSeen,
  queueBadgeCounts,
  QUEUE_BY_PATH,
  QUEUE_KEYS,
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
