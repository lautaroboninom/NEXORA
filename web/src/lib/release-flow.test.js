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

test("las OS sin presupuesto emitido no se ven afectadas", () => {
  for (const presupuesto_estado of [undefined, "", "pendiente", "no_aplica"]) {
    const flow = getReleaseFlow({ id: 1, estado: "reparado", presupuesto_estado });
    assert.equal(flow.blockedReason, "", `bloqueó con presupuesto ${presupuesto_estado}`);
  }
});

test("una OS ya liberada puede reimprimir aunque el presupuesto siga sin aprobar", () => {
  const flow = getReleaseFlow({ id: 29666, estado: "liberado", presupuesto_estado: "presupuestado" });

  assert.equal(flow.isReprint, true);
  assert.equal(flow.blockedReason, "");
  assert.equal(flow.canSubmit, true);
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
