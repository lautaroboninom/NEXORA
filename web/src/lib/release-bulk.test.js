import test from "node:test";
import assert from "node:assert/strict";
import {
  BULK_RELEASE_HEADER_EMITIDOS,
  BULK_RELEASE_HEADER_FALLIDOS,
  bulkReleaseFailuresFromError,
  bulkReleasePath,
  classifyReleaseSelection,
  parseBulkReleaseHeaders,
  summarizeBulkRelease,
} from "./release-bulk.js";

test("deja pasar los estados listos para liberar", () => {
  const rows = [
    { id: 1, estado: "reparado", presupuesto_estado: "aprobado" },
    { id: 2, estado: "controlado_sin_defecto", presupuesto_estado: "no_aplica" },
    { id: 3, estado: "no_se_repara" },
  ];
  const { ready, blocked } = classifyReleaseSelection(rows);
  assert.deepEqual(ready.map((item) => item.id), [1, 2, 3]);
  assert.equal(blocked.length, 0);
});

test("bloquea lo que todavía no se puede liberar y explica por qué", () => {
  const rows = [
    { id: 1, estado: "reparado", presupuesto_estado: "aprobado" },
    { id: 2, estado: "ingresado" },
    { id: 3, estado: "vendido_pendiente_entrega" },
    { id: 4, estado: "liberado" },
    { estado: "reparado" },
  ];
  const { ready, blocked } = classifyReleaseSelection(rows);

  assert.deepEqual(ready.map((item) => item.id), [1]);
  assert.deepEqual(
    blocked.map((item) => item.id),
    [2, 3, 4, null]
  );
  assert.match(blocked[0].reason, /resolución de salida/);
  assert.match(blocked[1].reason, /venta pendiente de entrega/);
  assert.match(blocked[2].reason, /ya fue emitida/);
  assert.match(blocked[3].reason, /identificar la OS/);
});

test("bloquea una OS lista mientras continúa derivada", () => {
  const { ready, blocked } = classifyReleaseSelection([
    {
      id: 44,
      estado: "reparado",
      resolucion: "reparado",
      derivacion_activa: true,
      derivacion_proveedor: "Laboratorio externo",
    },
  ]);

  assert.equal(ready.length, 0);
  assert.equal(blocked.length, 1);
  assert.match(blocked[0].reason, /sigue derivado/i);
});

test("una resolución ya cargada alcanza aunque el estado no sea final", () => {
  const rows = [{ id: 7, estado: "reparar", resolucion: "no_reparado" }];
  const { ready } = classifyReleaseSelection(rows);
  assert.deepEqual(ready.map((item) => item.id), [7]);
});

test("no repite la misma OS elegida dos veces", () => {
  const rows = [
    { id: 5, estado: "reparado", presupuesto_estado: "aprobado" },
    { id: 5, estado: "reparado", presupuesto_estado: "aprobado" },
  ];
  const { ready } = classifyReleaseSelection(rows);
  assert.equal(ready.length, 1);
});

test("lee el resultado del lote desde los headers", () => {
  const headers = new Map([
    [BULK_RELEASE_HEADER_EMITIDOS, "12,15"],
    [
      BULK_RELEASE_HEADER_FALLIDOS,
      JSON.stringify([{ id: 20, status: 409, detail: "No se puede liberar sin resolución" }]),
    ],
  ]);
  const { emitidos, fallidos } = parseBulkReleaseHeaders({
    get: (name) => headers.get(name) || "",
  });

  assert.deepEqual(emitidos, [12, 15]);
  assert.equal(fallidos.length, 1);
  assert.equal(fallidos[0].id, 20);
  assert.equal(fallidos[0].detail, "No se puede liberar sin resolución");
});

test("sin headers no inventa resultados", () => {
  assert.deepEqual(parseBulkReleaseHeaders(null), { emitidos: [], fallidos: [] });
  assert.deepEqual(parseBulkReleaseHeaders({ get: () => "" }), { emitidos: [], fallidos: [] });
  assert.deepEqual(parseBulkReleaseHeaders({ get: () => "{no es json" }).fallidos, []);
});

test("cuando falla todo el lote los motivos vienen en el cuerpo del error", () => {
  const error = { data: { fallidos: [{ id: 3, detail: "sin resolución" }] } };
  assert.deepEqual(bulkReleaseFailuresFromError(error), [{ id: 3, detail: "sin resolución" }]);
  assert.deepEqual(bulkReleaseFailuresFromError(new Error("boom")), []);
});

test("el resumen sólo menciona lo que pasó", () => {
  assert.equal(
    summarizeBulkRelease({ requested: 3, emitidos: [1, 2, 3] }),
    "Elegidas: 3. Liberadas: 3."
  );
  assert.equal(
    summarizeBulkRelease({ requested: 4, emitidos: [1, 2], fallidos: [{ id: 3 }], excluidos: 1 }),
    "Elegidas: 4. Liberadas: 2. Excluidas antes de enviar: 1. Fallaron: 1."
  );
});

test("con una sola OS usa el endpoint individual", () => {
  assert.equal(bulkReleasePath([42]), "/api/ingresos/42/remito/");
  assert.equal(
    bulkReleasePath([42, 43]),
    "/api/ingresos/remitos-salida/?ids=42%2C43&skip_errors=1"
  );
  assert.equal(bulkReleasePath([]), "");
});

// El lote sale del mismo botón que el modal de una sola OS, pero no tenía su gate: una
// resolución 'cambio' sin el equipo vinculado se liberaba igual.
test("bloquea un cambio de equipo sin el equipo elegido", () => {
  const { ready, blocked } = classifyReleaseSelection([
    { id: 7, estado: "reparado", presupuesto_estado: "aprobado", resolucion: "cambio" },
  ]);
  assert.equal(ready.length, 0);
  assert.match(blocked[0].reason, /cambio de equipo/i);
});

test("deja pasar el cambio ya registrado y la parte de pago", () => {
  const { ready, blocked } = classifyReleaseSelection([
    { id: 8, estado: "reparado", presupuesto_estado: "aprobado", resolucion: "cambio", serial_cambio: "NS-123", device_cambio_id: 5 },
    { id: 9, estado: "reparado", presupuesto_estado: "aprobado", resolucion: "cambio", cambio_sin_entrega: true },
  ]);
  assert.deepEqual(ready.map((item) => item.id), [8, 9]);
  assert.equal(blocked.length, 0);
});
