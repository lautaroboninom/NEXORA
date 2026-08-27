import test from "node:test";
import assert from "node:assert/strict";

import {
  DEVICE_ACTION,
  canRequestDeviceBaja,
  describeBejermanStockJob,
  getDeviceActions,
  lastIngresoIdOf,
} from "./device-actions.js";

const equipoMg = (extra = {}) => ({
  id: 29223,
  last_ingreso_id: 29630,
  last_ingreso_estado: "ingresado",
  es_propietario_mg: true,
  numero_interno: "MG 7298",
  numero_serie: "H2123405080",
  ...extra,
});

const keys = (row, ctx) => getDeviceActions(row, ctx).map((a) => a.key);
const find = (row, ctx, key) => getDeviceActions(row, ctx).find((a) => a.key === key);

test("con permiso de baja/alta ofrece dar de baja sobre un MG en taller", () => {
  const result = keys(equipoMg(), { canBajaAlta: true });
  assert.ok(result.includes(DEVICE_ACTION.DAR_BAJA));
  assert.ok(!result.includes(DEVICE_ACTION.DAR_ALTA));
  assert.ok(!result.includes(DEVICE_ACTION.SOLICITAR_BAJA));
});

test("si el último ingreso ya está en baja ofrece el alta y no la baja", () => {
  const result = keys(equipoMg({ last_ingreso_estado: "baja" }), { canBajaAlta: true });
  assert.ok(result.includes(DEVICE_ACTION.DAR_ALTA));
  assert.ok(!result.includes(DEVICE_ACTION.DAR_BAJA));
});

test("sin permiso directo ofrece solicitar, nunca las dos variantes juntas", () => {
  const result = keys(equipoMg(), { canBajaAlta: false, canRequestBaja: true });
  assert.deepEqual(
    result.filter((k) => k.includes("baja") || k.includes("alta")),
    [DEVICE_ACTION.SOLICITAR_BAJA],
  );
});

test("sin permiso directo y con el ingreso ya en baja no ofrece solicitar", () => {
  const result = keys(equipoMg({ last_ingreso_estado: "baja" }), {
    canBajaAlta: false,
    canRequestBaja: true,
  });
  assert.ok(!result.includes(DEVICE_ACTION.SOLICITAR_BAJA));
});

test("un equipo de cliente no ofrece ninguna variante de baja", () => {
  const row = equipoMg({ es_propietario_mg: false });
  const result = keys(row, { canBajaAlta: true, canRequestBaja: true });
  assert.ok(!result.includes(DEVICE_ACTION.DAR_BAJA));
  assert.ok(!result.includes(DEVICE_ACTION.DAR_ALTA));
  assert.ok(!result.includes(DEVICE_ACTION.SOLICITAR_BAJA));
});

test("sin ingresos registrados la baja se ofrece deshabilitada y explicada", () => {
  const row = equipoMg({ last_ingreso_id: null });
  const baja = find(row, { canBajaAlta: true }, DEVICE_ACTION.DAR_BAJA);
  assert.ok(baja);
  assert.equal(baja.disabled, true);
  assert.match(baja.title, /no tiene ingresos/i);

  const verIngreso = find(row, { canBajaAlta: true }, DEVICE_ACTION.VER_INGRESO);
  assert.equal(verIngreso.disabled, true);
});

test("lastIngresoIdOf no confunde el id del equipo con el de la OS", () => {
  assert.equal(lastIngresoIdOf({ id: 29223 }), 0);
  assert.equal(lastIngresoIdOf({ id: 29223, last_ingreso_id: 29630 }), 29630);
});

test("marcar vendido exige número interno y propiedad MG", () => {
  assert.ok(keys(equipoMg(), { canEdit: true }).includes(DEVICE_ACTION.VENTA_MG));
  assert.ok(!keys(equipoMg({ numero_interno: "" }), { canEdit: true }).includes(DEVICE_ACTION.VENTA_MG));
  assert.ok(
    !keys(equipoMg({ es_propietario_mg: false }), { canEdit: true }).includes(DEVICE_ACTION.VENTA_MG),
  );
});

test("sin permisos sólo queda ver el ingreso", () => {
  assert.deepEqual(keys(equipoMg(), {}), [DEVICE_ACTION.VER_INGRESO]);
});

test("imprimir QR se deshabilita sin identificador y mientras genera", () => {
  const sinId = find(equipoMg({ numero_serie: "", numero_interno: "" }), { canPrintBarcode: true }, DEVICE_ACTION.IMPRIMIR_QR);
  assert.equal(sinId.disabled, true);
  const generando = find(equipoMg(), { canPrintBarcode: true, barcodeBusy: true }, DEVICE_ACTION.IMPRIMIR_QR);
  assert.equal(generando.disabled, true);
  assert.match(generando.label, /Preparando/);
});

test("include filtra el subconjunto pedido", () => {
  const result = keys(equipoMg(), {
    canEdit: true,
    canBajaAlta: true,
    include: [DEVICE_ACTION.DAR_BAJA],
  });
  assert.deepEqual(result, [DEVICE_ACTION.DAR_BAJA]);
});

test("el mensaje del job Bejerman distingue cada estado", () => {
  assert.match(describeBejermanStockJob({ status: "succeeded" }, "Baja"), /completada/);
  assert.match(
    describeBejermanStockJob({ status: "blocked", last_error: "sin stock" }, "Baja"),
    /bloqueada: sin stock.*cola Bejerman/s,
  );
  assert.match(describeBejermanStockJob({ status: "not_queued" }, "Alta"), /Alta registrada.*no se pudo encolar/is);
  assert.match(describeBejermanStockJob({ status: "pending" }, "Baja"), /en cola/);
  assert.equal(describeBejermanStockJob(null, "Baja"), "Baja registrada.");
});

test("canRequestDeviceBaja acepta cualquiera de los permisos operativos", () => {
  assert.equal(canRequestDeviceBaja({ permissions: { "action.ingreso.edit_basics": true } }), true);
  assert.equal(canRequestDeviceBaja({ permissions: { "action.presupuesto.manage": true } }), true);
  assert.equal(canRequestDeviceBaja({ rol: "sin_rol", permissions: {} }), false);
});
