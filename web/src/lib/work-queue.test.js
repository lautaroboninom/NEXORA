import test from "node:test";
import assert from "node:assert/strict";
import {
  matchesWorkQueueFilters,
  WORK_QUEUE_EMPTY_FILTER,
  workQueueProperty,
} from "./work-queue.js";

test("distingue las tres categorías de propiedad", () => {
  assert.equal(
    workQueueProperty({ es_propietario_mg: true, tiene_codigo_mg: true, numero_interno: "MG 0001" }),
    "MG BIO",
  );
  assert.equal(
    workQueueProperty({ es_propietario_mg: false, numero_serie: "SERIE-1" }),
    "Cliente",
  );
  assert.equal(
    workQueueProperty({ tiene_codigo_mg: true, mg_inactivo_venta: true, numero_interno: "MG 0002" }),
    "Cliente (MG histórico)",
  );
});

test("intersecta filtros y reconoce técnico y presupuesto vacíos", () => {
  const row = {
    id: 42,
    razon_social: "Clínica Central",
    marca: "ResMed",
    modelo: "AirSense 10",
    tipo_equipo: "CPAP",
    numero_interno: "MG 0042",
    es_propietario_mg: true,
    tiene_codigo_mg: true,
    asignado_a: null,
    tecnico: "",
    presupuesto_estado: "",
    estado: "ingresado",
  };

  assert.equal(matchesWorkQueueFilters(row, {
    q: "airsense",
    cliente: "clínica",
    propiedad: "MG BIO",
    marca: "ResMed",
    tecnico: WORK_QUEUE_EMPTY_FILTER,
    presupuesto: WORK_QUEUE_EMPTY_FILTER,
  }), true);
  assert.equal(matchesWorkQueueFilters(row, {
    cliente: "clínica",
    propiedad: "Cliente",
  }), false);
});
