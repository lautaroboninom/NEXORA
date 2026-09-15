import test from "node:test";
import assert from "node:assert/strict";
import {
  deviceCustomerSubtitle,
  deviceIdentifierPartsOf,
  ingresoCustomerBaseName,
  ingresoCustomerName,
  isDemoReturn,
  MG_CODE_RE,
  propiedadEquipoLabelOf,
  bejermanDepositForLocation,
  canonicalLocationName,
  isSepidLocationName,
  locationDisplayName,
} from "./ui-helpers.js";

test("el número interno se reconoce con o sin espacio y con ceros adelante", () => {
  // Espejo de `service/tests/test_mg_codes.py`.
  for (const valor of ["MG1678", "MG 1678", "MG  1678", "mg 1678", "MG 001678", "NM 12", "CE102"]) {
    assert.equal(MG_CODE_RE.test(valor), true, valor);
  }
  for (const valor of ["", "MG", "MG SM-004", "MZJ5S121244", "1234", "MG 1234567", "MG-1234"]) {
    assert.equal(MG_CODE_RE.test(valor), false, valor);
  }
});

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

// La OS 29629: el equipo volvió de una demo, así que es nuestro aunque el ingreso siga
// mostrando a OXICASTHOMECARE (quien lo devolvió) y el equipo no tenga número MG.
const DEVOLUCION_DEMO = {
  numero_serie: "MA26040480026",
  numero_interno: "",
  motivo: "devolución demo",
  estado: "controlado_sin_defecto",
};

test("una devolución de demo figura como propiedad de MG BIO", () => {
  assert.equal(isDemoReturn(DEVOLUCION_DEMO), true);
  assert.equal(propiedadEquipoLabelOf(DEVOLUCION_DEMO), "MG BIO");
  // El enum convivió con y sin tilde.
  assert.equal(propiedadEquipoLabelOf({ ...DEVOLUCION_DEMO, motivo: "Devolucion Demo" }), "MG BIO");
});

test("vendida la demo, la propiedad vuelve a ser del cliente", () => {
  const vendida = { ...DEVOLUCION_DEMO, estado: "vendido_entregado" };
  assert.equal(propiedadEquipoLabelOf(vendida), "Cliente");
});

test("otros motivos no cambian la propiedad", () => {
  assert.equal(isDemoReturn({ motivo: "reparación" }), false);
  assert.equal(propiedadEquipoLabelOf({ numero_serie: "X1", motivo: "reparación" }), "Cliente");
});

test("la columna Cliente muestra el dueño detrás de un cliente contenedor", () => {
  const particular = { razon_social: "Particular", propietario_nombre: "Juan Pérez" };
  assert.equal(ingresoCustomerName(particular), "Juan Pérez");
  assert.equal(ingresoCustomerBaseName(particular), "Particular");
  assert.equal(deviceCustomerSubtitle(particular), "Cliente base: Particular");

  const mgbio = { razon_social: "MG BIO S.R.L.", propietario_nombre: "Ana Gómez" };
  assert.equal(ingresoCustomerName(mgbio), "Ana Gómez");
});

test("un cliente real sigue mandando aunque el equipo tenga dueño cargado", () => {
  const row = { razon_social: "SEDESA S.A.", propietario_nombre: "Juan Pérez" };
  assert.equal(ingresoCustomerName(row), "SEDESA S.A.");
  // En los listados no se anota el dueño de un cliente real: sería ruido fila por fila.
  assert.equal(ingresoCustomerBaseName(row), "");
  assert.equal(deviceCustomerSubtitle(row), "Dueño: Juan Pérez");
});

test("sin dueño cargado el particular se sigue viendo como antes", () => {
  assert.equal(ingresoCustomerName({ razon_social: "Particular" }), "Particular");
  // Vacío y no "-": los filtros por columna ofrecen "(sin dato)" con eso.
  assert.equal(ingresoCustomerName({}), "");
});

test("también lee los listados que traen el cliente en `cliente`", () => {
  assert.equal(
    ingresoCustomerName({ cliente: "Particular", propietario_nombre: "Juan Pérez" }),
    "Juan Pérez"
  );
});

test("Sepid y sus aliases históricos se reconocen como depósito Sepid", () => {
  for (const alias of [
    "Sepid",
    "sepid",
    "Depósito SEPID",
    "deposito sepid",
    "deposito sepid val",
    "Sepid (VAL)",
    "deposito sepid (val)",
    "sepid val",
    "deposito sepid s.a.",
    "sepid sa",
    "deposito central",
    "deposito central sepid",
    "valdenegro",
    "deposito valdenegro",
    "valdenegro 4578",
  ]) {
    assert.equal(isSepidLocationName(alias), true, alias);
    assert.equal(canonicalLocationName(alias), "Sepid", alias);
    assert.equal(bejermanDepositForLocation(alias), "VAL", alias);
    assert.equal(locationDisplayName(alias), "Sepid (VAL)", alias);
  }

  for (const otro of ["Taller", "Sarmiento", "Estantería de Alquiler", "-", ""]) {
    assert.equal(isSepidLocationName(otro), false, otro);
  }
});

test("ubicaciones canónicas resuelven código Bejerman y etiqueta visual correcta", () => {
  assert.equal(bejermanDepositForLocation("Taller"), "STR");
  assert.equal(bejermanDepositForLocation("Taller (STR)"), "STR");
  assert.equal(locationDisplayName("Taller"), "Taller");

  assert.equal(bejermanDepositForLocation("Sarmiento"), "SAR");
  assert.equal(bejermanDepositForLocation("Sarmiento (SAR)"), "SAR");
  assert.equal(locationDisplayName("Sarmiento"), "Sarmiento (SAR)");

  assert.equal(bejermanDepositForLocation("Estantería de Alquiler"), "STL");
  assert.equal(bejermanDepositForLocation("estanteria alquiler"), "STL");
  assert.equal(locationDisplayName("Estantería de Alquiler"), "Estantería de Alquiler");

  assert.equal(bejermanDepositForLocation("-"), "");
  assert.equal(locationDisplayName("-"), "-");
});
