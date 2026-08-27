import test from "node:test";
import assert from "node:assert/strict";
import { deviceIdentifierPartsOf, propiedadEquipoLabelOf } from "./ui-helpers.js";

// La OS 29641 en producción: el equipo se le vendió a NOVAMED y su MG 4988
// quedó histórico, así que en los listados sólo tiene que verse el N/S.
const MG_HISTORICO = {
  numero_serie: "MZJ5S121621",
  numero_interno: "MG 4988",
  mg_estado: "inactivo_venta",
  mg_inactivo_venta: true,
};

const MG_PROPIO = {
  numero_serie: "MZJ5S121902",
  numero_interno: "MG 4700",
  mg_estado: "activo",
};

const valores = (row, opts) =>
  deviceIdentifierPartsOf(row, "-", opts).identifiers.map((item) => item.value);

test("un MG histórico no muestra su número interno en los listados", () => {
  const parts = deviceIdentifierPartsOf(MG_HISTORICO, "-");
  assert.deepEqual(valores(MG_HISTORICO), ["MZJ5S121621"]);
  assert.equal(parts.primary, "MZJ5S121621");
  assert.equal(parts.secondary, "");
});

test("el detalle de la OS sí puede mostrar el MG histórico", () => {
  const opts = { includeHistoricalMg: true };
  assert.deepEqual(valores(MG_HISTORICO, opts), ["MZJ5S121621", "MG 4988"]);
  assert.equal(
    deviceIdentifierPartsOf(MG_HISTORICO, "-", opts).secondary,
    "MG histórico: MG 4988"
  );
});

test("un MG activo sigue mostrando su número interno primero", () => {
  const parts = deviceIdentifierPartsOf(MG_PROPIO, "-");
  assert.deepEqual(valores(MG_PROPIO), ["MZJ5S121902", "MG 4700"]);
  assert.equal(parts.primary, "MG 4700");
});

test("si el MG histórico es el único identificador, se muestra igual", () => {
  const sinSerie = { ...MG_HISTORICO, numero_serie: "" };
  assert.deepEqual(valores(sinSerie), ["MG 4988"]);
  assert.equal(deviceIdentifierPartsOf(sinSerie, "-").primary, "MG 4988");
});

test("la propiedad sigue avisando que el equipo fue un MG", () => {
  assert.equal(propiedadEquipoLabelOf(MG_HISTORICO), "Cliente (MG histórico)");
  assert.equal(propiedadEquipoLabelOf(MG_PROPIO), "MG BIO");
});
