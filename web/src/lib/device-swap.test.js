import test from "node:test";
import assert from "node:assert/strict";

import {
  SWAP_DESTINO,
  describeSwapSummary,
  getDeviceSwapState,
  swapActionLabel,
  swapConfirmMatches,
  swapDestinoLabel,
  swapDestinoResumen,
  SWAP_DESTINO_OPTIONS,
  SWAP_DESTINOS_PATRIMONIALES,
} from "./device-swap.js";

const osConCambio = (extra = {}) => ({
  id: 29630,
  estado: "reparado",
  resolucion: "cambio",
  cambio_equipo: {
    device_cambio_id: 4211,
    serial_cambio: "H2123405080 | MG 7298",
    equipo_entregado: "H2123405080 | MG 7298",
    destino_viejo: SWAP_DESTINO.MG,
    vinculado: true,
  },
  ...extra,
});

test("una OS sin cambio no reporta cambio pero sí deja registrarlo", () => {
  const state = getDeviceSwapState({ estado: "reparado", resolucion: "reparado" }, { canSwap: true });
  assert.equal(state.hasSwap, false);
  assert.equal(state.canRegister, true);
  assert.equal(state.canUndo, false);
});

test("sin permiso no se puede registrar ni deshacer", () => {
  const state = getDeviceSwapState(osConCambio(), { canSwap: false });
  assert.equal(state.hasSwap, true);
  assert.equal(state.canRegister, false);
  assert.equal(state.canUndo, false);
  assert.match(state.blockedReason, /permiso/i);
});

test("una OS ya entregada no admite registrar el cambio", () => {
  const state = getDeviceSwapState(osConCambio({ estado: "entregado" }), { canSwap: true });
  assert.equal(state.canRegister, false);
  assert.equal(state.canUndo, false);
  assert.match(state.blockedReason, /entregado/);
});

test("lee el cambio desde las columnas planas de un listado", () => {
  // Reparados/Aprobados no traen `cambio_equipo`, sólo las columnas sueltas.
  const state = getDeviceSwapState(
    {
      estado: "reparado",
      resolucion: "cambio",
      serial_cambio: "MZJ5S 121220",
      device_cambio_id: 991,
      cambio_destino_viejo: SWAP_DESTINO.DESGUACE,
    },
    { canSwap: true }
  );
  assert.equal(state.hasSwap, true);
  assert.equal(state.isLinked, true);
  assert.equal(state.deliveredLabel, "MZJ5S 121220");
  assert.equal(state.destino, SWAP_DESTINO.DESGUACE);
});

test("un cambio histórico sin FK se muestra pero no cuenta como vinculado", () => {
  const state = getDeviceSwapState(
    { estado: "liberado", resolucion: "cambio", serial_cambio: "MG 1234" },
    { canSwap: true }
  );
  assert.equal(state.hasSwap, true);
  assert.equal(state.isLinked, false);
  assert.equal(state.deliveredLabel, "MG 1234");
  // Sin destino cargado se asume pendiente, que es lo que hace el backfill.
  assert.equal(state.destino, SWAP_DESTINO.PENDIENTE);
});

test("el resumen nombra el equipo que sale y aclara que el viejo queda", () => {
  const txt = describeSwapSummary(osConCambio());
  assert.match(txt, /Se entrega H2123405080 \| MG 7298/);
  assert.match(txt, /queda en SEPID/);
  assert.match(txt, /equipo propio de SEPID/i);
});

test("el resumen queda vacío si no hay cambio", () => {
  assert.equal(describeSwapSummary({ estado: "reparado", resolucion: "reparado" }), "");
});

test("la confirmación acepta cualquiera de las dos partes del snapshot", () => {
  const etiqueta = "H2123405080 | MG 7298";
  assert.equal(swapConfirmMatches("H2123405080", etiqueta), true);
  assert.equal(swapConfirmMatches("mg 7298", etiqueta), true);
  assert.equal(swapConfirmMatches("  MG 7298  ", etiqueta), true);
  assert.equal(swapConfirmMatches("MG 7299", etiqueta), false);
  assert.equal(swapConfirmMatches("", etiqueta), false);
});

// Parte de pago sin permuta: tomamos el equipo del cliente y no sale ninguno a cambio.
const osConParteDePago = (extra = {}) => ({
  id: 29700,
  estado: "reparado",
  resolucion: "cambio",
  cambio_equipo: {
    device_cambio_id: null,
    serial_cambio: "",
    equipo_entregado: "",
    destino_viejo: SWAP_DESTINO.MG,
    vinculado: false,
    sin_entrega: true,
  },
  ...extra,
});

test("una parte de pago cuenta como registrada aunque no salga ningún equipo", () => {
  const state = getDeviceSwapState(osConParteDePago(), { canSwap: true });
  // Sin la marca `sin_entrega` esto daría false y la OS quedaría ofreciendo registrar
  // un cambio que ya existe.
  assert.equal(state.hasSwap, true);
  assert.equal(state.sinEntrega, true);
  assert.equal(state.deliveredLabel, "");
  assert.equal(state.canUndo, true);
  assert.equal(state.destino, SWAP_DESTINO.MG);
});

test("la parte de pago también se lee desde las columnas planas", () => {
  const state = getDeviceSwapState(
    {
      estado: "liberado",
      resolucion: "cambio",
      cambio_sin_entrega: true,
      cambio_destino_viejo: SWAP_DESTINO.DESGUACE,
    },
    { canSwap: true }
  );
  assert.equal(state.hasSwap, true);
  assert.equal(state.sinEntrega, true);
  assert.equal(state.isLinked, false);
});

test("la parte de pago sobrevive a que la OS se marque reparada", () => {
  // Al equipo tomado se lo repara igual, y eso pisa la resolución. Si `hasSwap` dependiera
  // de `resolucion`, la pantalla ofrecería registrar de nuevo algo ya registrado y el
  // corte de stock se perdería.
  const state = getDeviceSwapState(osConParteDePago({ resolucion: "reparado" }), { canSwap: true });
  assert.equal(state.hasSwap, true);
  assert.equal(state.sinEntrega, true);
});

test("un cambio con equipo sí deja de contar si cambia la resolución", () => {
  // Ahí la permuta es el resultado de la OS: si se resolvió de otra forma, no hubo cambio.
  const state = getDeviceSwapState(osConCambio({ resolucion: "reparado" }), { canSwap: true });
  assert.equal(state.hasSwap, false);
});

test("un cambio con equipo no queda marcado como parte de pago", () => {
  assert.equal(getDeviceSwapState(osConCambio(), { canSwap: true }).sinEntrega, false);
});

test("el resumen de la parte de pago no promete ninguna entrega", () => {
  const txt = describeSwapSummary(osConParteDePago());
  assert.match(txt, /no se entrega ninguno/i);
  assert.doesNotMatch(txt, /Se entrega [A-Z0-9]/);
  assert.match(txt, /equipo propio de SEPID/i);
});

test("la etiqueta del menú distingue las tres situaciones", () => {
  const sinNada = getDeviceSwapState({ estado: "reparado" }, { canSwap: true });
  assert.equal(swapActionLabel(sinNada), "Cambio de equipo / parte de pago");
  assert.equal(
    swapActionLabel(getDeviceSwapState(osConCambio(), { canSwap: true })),
    "Ver / editar cambio de equipo"
  );
  assert.equal(
    swapActionLabel(getDeviceSwapState(osConParteDePago(), { canSwap: true })),
    "Ver / editar parte de pago"
  );
});

test("las etiquetas de destino cubren los cuatro casos", () => {
  // El combo tiene que decir "Propiedad de SEPID" en los tres destinos que cambian de
  // dueño: elegir mal era el malentendido que dejaba el equipo a nombre del cliente.
  assert.match(swapDestinoLabel(SWAP_DESTINO.MG), /^Propiedad de SEPID/);
  assert.match(swapDestinoLabel(SWAP_DESTINO.VENTA), /^Propiedad de SEPID/);
  assert.match(swapDestinoLabel(SWAP_DESTINO.DESGUACE), /^Propiedad de SEPID/);
  assert.match(swapDestinoLabel(SWAP_DESTINO.PENDIENTE), /^Se decide después/);
  assert.equal(swapDestinoLabel("otra_cosa"), "");
});

test("el destino de venta nombra el depósito al que va", () => {
  assert.match(swapDestinoLabel(SWAP_DESTINO.VENTA), /VAL/);
  assert.match(swapDestinoResumen(SWAP_DESTINO.VENTA), /venta/i);
  assert.match(swapDestinoResumen(SWAP_DESTINO.VENTA), /VAL/);
});

test("sólo 'pendiente' deja el equipo del lado del cliente", () => {
  // Espejo de DEVICE_SWAP_DESTINOS_PATRIMONIALES: el combo deshabilita para recepción
  // exactamente los destinos que el backend gatea con el permiso patrimonial.
  const patrimoniales = SWAP_DESTINO_OPTIONS.filter((o) => o.patrimonial).map((o) => o.value);
  assert.deepEqual(patrimoniales, SWAP_DESTINOS_PATRIMONIALES);
  assert.deepEqual(patrimoniales, ["mg", "venta", "desguace"]);
  assert.equal(SWAP_DESTINO_OPTIONS.some((o) => o.value === "pendiente" && o.patrimonial), false);
});

test("el estado dice si el equipo ya pasó a propiedad de SEPID", () => {
  const propio = getDeviceSwapState(
    osConCambio({ cambio_equipo: { ...osConCambio().cambio_equipo, destino_viejo: "venta" } }),
    { canSwap: true }
  );
  assert.equal(propio.esPropiedadSepid, true);

  const sinDefinir = getDeviceSwapState(
    osConCambio({ cambio_equipo: { ...osConCambio().cambio_equipo, destino_viejo: "pendiente" } }),
    { canSwap: true }
  );
  assert.equal(sinDefinir.esPropiedadSepid, false);
});
