import assert from "node:assert/strict";
import test from "node:test";

import { remitoJobError, waitForRemitoJob } from "./delivery-remito-job.js";

const sinEspera = { pollMs: 0, now: () => 0, deadlineMs: 0 };

test("devuelve el resultado cuando el trabajo ya está listo", async () => {
  const result = await waitForRemitoJob("drj-1", {
    ...sinEspera,
    fetchJob: async () => ({ status: "done", done: true, result: { remitoNumber: "00004-00005040" } }),
  });
  assert.equal(result.remitoNumber, "00004-00005040");
});

test("sigue sondeando mientras el trabajo está en curso", async () => {
  const respuestas = [
    { status: "pending", done: false },
    { status: "running", done: false, elapsedSeconds: 4 },
    { status: "done", done: true, result: { remitoNumber: "R-1" } },
  ];
  let consultas = 0;
  const progreso = [];
  const result = await waitForRemitoJob("drj-2", {
    ...sinEspera,
    onProgress: (p) => progreso.push(p.status),
    fetchJob: async () => respuestas[consultas++],
  });
  assert.equal(consultas, 3);
  assert.equal(result.remitoNumber, "R-1");
  assert.deepEqual(progreso, ["en cola", "emitiendo", "listo"]);
});

test("un trabajo fallado se convierte en el mismo error que tiraba el POST", async () => {
  await assert.rejects(
    waitForRemitoJob("drj-3", {
      ...sinEspera,
      fetchJob: async () => ({
        status: "error",
        done: true,
        error: { detail: "Falta stock", code: "DELIVERY_REMITO_STOCK", status: 409 },
      }),
    }),
    (error) => {
      assert.equal(error.message, "Falta stock");
      assert.equal(error.status, 409);
      assert.equal(error.code, "DELIVERY_REMITO_STOCK");
      return true;
    },
  );
});

test("un trabajo que quedó sin respuesta no se reporta como éxito", async () => {
  await assert.rejects(
    waitForRemitoJob("drj-4", {
      ...sinEspera,
      fetchJob: async () => ({
        status: "unknown",
        done: true,
        error: { detail: "La emisión quedó sin respuesta", code: "DELIVERY_REMITO_JOB_STALE", status: 409 },
      }),
    }),
    /sin respuesta/,
  );
});

test("corta cuando se cancela la espera", async () => {
  const controller = new AbortController();
  controller.abort();
  await assert.rejects(
    waitForRemitoJob("drj-5", { ...sinEspera, signal: controller.signal, fetchJob: async () => ({ status: "pending" }) }),
    /cancel/i,
  );
});

test("respeta el tope de espera sin dar el remito por perdido", async () => {
  let ahora = 0;
  await assert.rejects(
    waitForRemitoJob("drj-6", {
      pollMs: 0,
      deadlineMs: 1000,
      now: () => (ahora += 600),
      fetchJob: async () => ({ status: "running", done: false }),
    }),
    (error) => {
      assert.equal(error.code, "REMITO_JOB_TIMEOUT");
      assert.match(error.message, /sigue corriendo en el servidor/i);
      return true;
    },
  );
});

test("remitoJobError sobrevive a un payload vacío", () => {
  const error = remitoJobError(null);
  assert.equal(error.status, 400);
  assert.match(error.message, /No se pudo emitir/);
});
