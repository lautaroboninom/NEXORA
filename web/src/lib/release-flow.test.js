import test from "node:test";
import assert from "node:assert/strict";

import { getReleaseFlow } from "./release-flow.js";
import { classifyReleaseSelection } from "./release-bulk.js";

// Caso del 07/09/2026 (OS 29666): la OS llegó a "Listo para liberar" y se liberó con el
// presupuesto todavía en `presupuestado`. Este es el mismo corte que hace el backend en
// RemitoSalidaPdfView; acá se adelanta para que el operador lo vea antes de apretar.

test("no deja liberar con el presupuesto emitido y sin aprobar", () => {
  const flow = getReleaseFlow({ id: 29666, estado: "reparado", presupuesto_estado: "presupuestado" });

  assert.equal(flow.canSubmit, false);
  assert.match(flow.blockedReason, /sin aprobar/);
  assert.equal(flow.statusText, flow.blockedReason);
});

test("el presupuesto aprobado o rechazado sigue liberando", () => {
  for (const presupuesto_estado of ["aprobado", "rechazado"]) {
    const flow = getReleaseFlow({ id: 1, estado: "reparado", presupuesto_estado });
    assert.equal(flow.blockedReason, "", `bloqueó con presupuesto ${presupuesto_estado}`);
    assert.equal(flow.canSubmit, true);
  }
});

// Caso del 09/09/2026 (OS 29639): un aspirador de un cliente, sin garantía, reparado y
// liberado el mismo día con el presupuesto todavía en `pendiente` -ni siquiera emitido-.
// El corte del 07/09 sólo miraba `presupuestado`, así que esta OS pasó de largo.
test("no deja liberar la reparación de un cliente que nadie presupuestó", () => {
  for (const presupuesto_estado of [undefined, "", "pendiente"]) {
    const flow = getReleaseFlow({ id: 29639, estado: "reparado", presupuesto_estado, motivo: "reparación" });

    assert.equal(flow.canSubmit, false, `dejó liberar con presupuesto ${presupuesto_estado}`);
    assert.match(flow.blockedReason, /no tiene presupuesto/);
  }
});

test("el \"No aplica\" es la salida para el trabajo que no se cobra", () => {
  const flow = getReleaseFlow({ id: 1, estado: "reparado", presupuesto_estado: "no_aplica" });

  assert.equal(flow.blockedReason, "");
  assert.equal(flow.canSubmit, true);
});

test("lo que no se le cobra a nadie sigue saliendo sin presupuesto", () => {
  // Garantías, equipos nuestros, alquileres y demos se reparan sin presupuestar: el corte
  // no los puede frenar o el taller se queda con los equipos adentro.
  const casos = [
    { nombre: "garantía de fábrica", row: { garantia_fabrica: true } },
    { nombre: "garantía de reparación", row: { garantia_reparacion: true } },
    { nombre: "equipo propio", row: { numero_interno: "MG 1234" } },
    // Mismo criterio que el backend (`mg_codes`): sin espacio o con ceros adelante sigue siendo
    // nuestro. Con el tope viejo de 4 dígitos, "MG 001678" pedía presupuesto.
    { nombre: "equipo propio sin espacio", row: { numero_interno: "MG1234" } },
    { nombre: "equipo propio con ceros adelante", row: { numero_interno: "MG 001678" } },
    { nombre: "reparación de alquiler", row: { motivo: "reparación alquiler" } },
    { nombre: "devolución de demo", row: { motivo: "devolución demo" } },
  ];

  for (const { nombre, row } of casos) {
    const flow = getReleaseFlow({ id: 1, estado: "reparado", presupuesto_estado: "pendiente", ...row });
    assert.equal(flow.blockedReason, "", `bloqueó ${nombre}`);
    assert.equal(flow.canSubmit, true, `no dejó liberar ${nombre}`);
  }
});

test("el MG vendido vuelve a necesitar presupuesto", () => {
  // Dejó de ser nuestro: la reparación se le cobra al cliente que lo compró.
  const flow = getReleaseFlow({
    id: 1,
    estado: "reparado",
    presupuesto_estado: "pendiente",
    numero_interno: "MG 1234",
    mg_estado: "inactivo_venta",
  });

  assert.equal(flow.canSubmit, false);
  assert.match(flow.blockedReason, /no tiene presupuesto/);
});

test("el equipo vuelve al cliente cuando no se repara", () => {
  // `no_se_repara` devuelve el equipo sin trabajo que cobrar y no frena por presupuesto pendiente.
  const flow = getReleaseFlow({ id: 1, estado: "no_se_repara", presupuesto_estado: "pendiente" });
  assert.equal(flow.blockedReason, "");
  assert.equal(flow.canSubmit, true);
});

test("controlado sin defecto en equipo de cliente requiere presupuesto cerrado para salir", () => {
  // En clientes, controlar sin defecto tiene trabajo a presupuestar/cobrar (revisión técnica):
  // debe estar aprobado, rechazado o marcado explícitamente como no_aplica.
  const flowPendiente = getReleaseFlow({ id: 1, estado: "controlado_sin_defecto", presupuesto_estado: "pendiente" });
  assert.equal(flowPendiente.canSubmit, false);
  assert.match(flowPendiente.blockedReason, /no tiene presupuesto/);

  const flowNoAplica = getReleaseFlow({ id: 1, estado: "controlado_sin_defecto", presupuesto_estado: "no_aplica" });
  assert.equal(flowNoAplica.blockedReason, "");
  assert.equal(flowNoAplica.canSubmit, true);

  const flowPropio = getReleaseFlow({ id: 1, estado: "controlado_sin_defecto", presupuesto_estado: "pendiente", numero_interno: "MG 1234" });
  assert.equal(flowPropio.blockedReason, "");
  assert.equal(flowPropio.canSubmit, true);
});

test("una OS ya liberada puede reimprimir aunque el presupuesto siga sin aprobar", () => {
  const flow = getReleaseFlow({ id: 29666, estado: "liberado", presupuesto_estado: "presupuestado" });

  assert.equal(flow.isReprint, true);
  assert.equal(flow.blockedReason, "");
  assert.equal(flow.canSubmit, true);
});

// Caso del 08/09/2026 (OS 29643): un MG de alquiler reparado se vendió desde la hoja de
// servicio, la OS saltó a `vendido_pendiente_entrega` y el modal la bloqueaba entera. El
// backend nunca la bloqueó -está en ESTADOS_SALIDA_YA_EMITIDA- y el papel del trabajo hecho
// hace falta en el mostrador, así que se imprime sin tocar la OS.
test("una venta pendiente de entrega imprime la orden sin liberar", () => {
  const flow = getReleaseFlow({ id: 29643, estado: "vendido_pendiente_entrega", resolucion: "reparado" });

  assert.equal(flow.blockedReason, "");
  assert.equal(flow.isSalePending, true);
  assert.equal(flow.isReprint, true);
  assert.equal(flow.canSubmit, true);
  assert.match(flow.statusText, /Marcar venta entregada/);
});

test("el lote no barre una venta pendiente de entrega", () => {
  const { ready, blocked } = classifyReleaseSelection([
    { id: 1, estado: "reparado", presupuesto_estado: "aprobado" },
    { id: 29643, estado: "vendido_pendiente_entrega", resolucion: "reparado" },
  ]);

  assert.deepEqual(ready.map((item) => item.id), [1]);
  assert.deepEqual(blocked.map((item) => item.id), [29643]);
  assert.match(blocked[0].reason, /venta pendiente de entrega/);
  assert.match(blocked[0].reason, /RT/);
});

test("el lote también aparta la OS con el presupuesto sin aprobar", () => {
  const rows = [
    { id: 1, estado: "reparado", presupuesto_estado: "aprobado" },
    { id: 2, estado: "reparado", presupuesto_estado: "presupuestado" },
  ];

  const { ready, blocked } = classifyReleaseSelection(rows);

  assert.deepEqual(ready.map((item) => item.id), [1]);
  assert.deepEqual(blocked.map((item) => item.id), [2]);
  assert.match(blocked[0].reason, /sin aprobar/);
});

test("un presupuesto anulado, emitido o desconocido no habilita la reparación cobrable", () => {
  for (const presupuesto_estado of ["anulado", "emitido", "desconocido"]) {
    assert.equal(getReleaseFlow({ estado: "reparado", presupuesto_estado }).canSubmit, false);
  }
});

test("una serie con aspecto de MG o una garantía falsa no eximen del presupuesto", () => {
  for (const extra of [{ numero_serie: "MG 1234" }, { garantia_fabrica: "false" }]) {
    assert.equal(getReleaseFlow({ estado: "reparado", presupuesto_estado: "pendiente", ...extra }).canSubmit, false);
  }
});

test("el lote aparta también la reparación sin presupuesto", () => {
  const { ready, blocked } = classifyReleaseSelection([
    { id: 1, estado: "reparado", presupuesto_estado: "aprobado" },
    { id: 2, estado: "reparado", presupuesto_estado: "pendiente", numero_interno: null },
  ]);
  assert.deepEqual(ready.map((item) => item.id), [1]);
  assert.deepEqual(blocked.map((item) => item.id), [2]);
});
