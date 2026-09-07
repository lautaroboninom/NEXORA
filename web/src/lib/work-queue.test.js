import test from "node:test";
import assert from "node:assert/strict";
import {
  matchesWorkQueueFilters,
  WORK_QUEUE_EMPTY_FILTER,
  WORK_QUEUE_SORT_COLUMNS,
  workQueueProperty,
} from "./work-queue.js";
import { workQueueFilterOptions } from "./work-queue-filter-options.js";

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

// La cola de trabajo tenía su propia copia de la regla de propiedad, y esa copia se quedó
// sin la rama de devolución de demo: la misma OS decía "Cliente" acá y "MG BIO" en la hoja
// de servicio. Ahora delega en `propiedadEquipoLabelOf`.
test("una devolución de demo también figura como MG BIO en la cola", () => {
  const row = { numero_serie: "MA26040480026", motivo: "devolución demo", estado: "controlado_sin_defecto" };
  assert.equal(workQueueProperty(row), "MG BIO");
  assert.equal(workQueueProperty({ ...row, estado: "vendido_entregado" }), "Cliente");
});

test("unifica modelo y variante en ordenación y opciones de filtro", () => {
  const rows = [
    { id: 1, modelo: "AirSense 10", variante: "AutoSet" },
    { id: 2, modelo: "Stellar 150" },
  ];

  assert.equal(WORK_QUEUE_SORT_COLUMNS.modelo.accessor(rows[0]), "AirSense 10 AutoSet");
  assert.equal(WORK_QUEUE_SORT_COLUMNS.modelo.accessor(rows[1]), "Stellar 150");

  assert.equal(matchesWorkQueueFilters(rows[0], { modelo: "AirSense 10 AutoSet" }), true);
  assert.equal(matchesWorkQueueFilters(rows[0], { modelo: "AirSense 10" }), true);
  assert.equal(matchesWorkQueueFilters(rows[0], { modelo: "Stellar 150" }), false);

  const options = workQueueFilterOptions(rows);
  assert.deepEqual(options.modelo, [
    { value: "AirSense 10 AutoSet", label: "AirSense 10 AutoSet" },
    { value: "Stellar 150", label: "Stellar 150" },
  ]);
});

