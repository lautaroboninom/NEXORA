// Cambio de equipo (permuta / parte de pago).
//
// El cliente trae su equipo, se le entrega otro y el suyo queda para SEPID. Son dos
// movimientos patrimoniales, no un campo de texto: acá vive la decisión de SI se puede
// registrar un cambio y CÓMO se describe; el modal y las pantallas sólo lo consumen.
// Mismo modelo que `release-flow.js`.
//
// La variante sin permuta —tomamos el equipo en parte de pago y no entregamos ninguno—
// usa el mismo circuito, con `sin_entrega`. Es el mismo corte de stock: bajo esta OS no
// sale nada para el cliente.
//
// Extensiones explícitas en los imports: este módulo también se importa desde los tests,
// que corren con `node --test` sin el resolutor de Vite.
import { RESOLUCION } from "./constants.js";

// Qué se hace con el equipo que el cliente deja. `PENDIENTE` es el único que no define
// nada; los otros tres son las razones por las que el equipo **pasa a propiedad de SEPID**,
// y lo que los diferencia es dónde termina la unidad en Bejerman.
export const SWAP_DESTINO = {
  PENDIENTE: "pendiente",
  MG: "mg",
  VENTA: "venta",
  DESGUACE: "desguace",
};

export const SWAP_DESTINO_LABELS = {
  [SWAP_DESTINO.PENDIENTE]: "Se decide después (sigue siendo del cliente)",
  [SWAP_DESTINO.MG]: "Propiedad de SEPID — queda como equipo propio",
  [SWAP_DESTINO.VENTA]: "Propiedad de SEPID — va para venta (pasa a VAL)",
  [SWAP_DESTINO.DESGUACE]: "Propiedad de SEPID — repuestos / desguace (pasa a DES)",
};

/**
 * Los destinos que pasan el equipo a nuestro patrimonio. El backend los exige con
 * `baja_alta` o `devices_preventivos.manage`: recepción registra la toma y jefatura decide
 * el destino. Se enumeran acá y no por "todo lo que no sea pendiente" para que agregar un
 * destino no autorizado por descuido sea imposible.
 */
export const SWAP_DESTINOS_PATRIMONIALES = [
  SWAP_DESTINO.MG,
  SWAP_DESTINO.VENTA,
  SWAP_DESTINO.DESGUACE,
];

/** Destinos que además mueven la unidad de depósito en Bejerman. */
export const SWAP_DESTINO_MOVIMIENTO = {
  [SWAP_DESTINO.VENTA]: "VAL",
  [SWAP_DESTINO.DESGUACE]: "DES",
};

export const SWAP_DESTINO_OPTIONS = Object.entries(SWAP_DESTINO_LABELS).map(
  ([value, label]) => ({ value, label, patrimonial: SWAP_DESTINOS_PATRIMONIALES.includes(value) })
);

/**
 * Versión corta, en minúscula y sin el prefijo repetido, para meter el destino adentro de
 * una frase ("el equipo recibido ..."). La larga es para el combo, donde el prefijo
 * "Propiedad de SEPID" es justamente lo que hay que ver antes de elegir.
 */
export const SWAP_DESTINO_SHORT_LABELS = {
  [SWAP_DESTINO.PENDIENTE]: "todavía sin destino",
  [SWAP_DESTINO.MG]: "queda como equipo propio de SEPID",
  [SWAP_DESTINO.VENTA]: "pasa a SEPID y va para venta (VAL)",
  [SWAP_DESTINO.DESGUACE]: "pasa a SEPID y va a repuestos (DES)",
};

export const swapDestinoLabel = (value) =>
  SWAP_DESTINO_LABELS[String(value ?? "").trim().toLowerCase()] ?? "";

export const swapDestinoResumen = (value) =>
  SWAP_DESTINO_SHORT_LABELS[String(value ?? "").trim().toLowerCase()] ?? "";

// Estados en los que la OS ya cerró su circuito: registrar un cambio ahí llegaría tarde
// y encima dispararía movimientos de stock sobre un equipo que ya se fue. Espejo del
// chequeo de `IngresoCambioEquipoView.post`.
const CLOSED_STATES = new Set(["entregado", "baja", "vendido_entregado", "alquilado"]);

const text = (value) => String(value ?? "").trim();
const lower = (value) => text(value).toLowerCase();

/** Datos del cambio tal como los devuelve el back, vengan del detalle o de un listado. */
function swapDataOf(row) {
  const detalle = row?.cambio_equipo;
  if (detalle && typeof detalle === "object" && Object.keys(detalle).length) return detalle;
  // Los listados no traen el objeto completo: se arma con las columnas planas.
  const serial = text(row?.serial_cambio);
  const sinEntrega = Boolean(row?.cambio_sin_entrega);
  if (!serial && !row?.device_cambio_id && !sinEntrega) return null;
  return {
    device_cambio_id: row?.device_cambio_id ?? null,
    serial_cambio: serial,
    equipo_entregado: serial,
    destino_viejo: text(row?.cambio_destino_viejo),
    vinculado: Boolean(row?.device_cambio_id),
    sin_entrega: sinEntrega,
  };
}

/**
 * Estado del cambio de equipo de una OS.
 *
 * @param {object} row  fila de ingreso (detalle o listado)
 * @param {object} ctx
 *   canSwap — permiso `action.ingreso.device_swap`
 */
export function getDeviceSwapState(row = {}, { canSwap = false } = {}) {
  const estado = lower(row?.estado);
  const esCambio = lower(row?.resolucion) === RESOLUCION.CAMBIO;
  const data = swapDataOf(row);
  const deliveredLabel = text(data?.equipo_entregado || data?.serial_cambio);
  const sinEntrega = Boolean(data?.sin_entrega);
  // Una parte de pago no tiene equipo de salida, así que no se la puede reconocer por la
  // etiqueta: si se pidiera `deliveredLabel` quedaría como "sin registrar" para siempre.
  //
  // Tampoco depende de `resolucion`: tomar el equipo es un hecho patrimonial, y al equipo
  // tomado se lo repara igual, con lo que la resolución termina siendo 'reparado'.
  const hasSwap = Boolean((esCambio && deliveredLabel) || sinEntrega);
  const destino = lower(data?.destino_viejo) || SWAP_DESTINO.PENDIENTE;

  let blockedReason = "";
  if (!canSwap) {
    blockedReason = "No tenés permiso para registrar un cambio de equipo.";
  } else if (CLOSED_STATES.has(estado)) {
    blockedReason = `La orden ya está en estado ${estado}: el cambio se registra antes de la salida.`;
  }

  return {
    hasSwap,
    // Los cambios viejos (anteriores a la FK) sólo tienen el texto: se muestran igual,
    // pero no se pueden tratar como un equipo real.
    isLinked: Boolean(data?.vinculado ?? data?.device_cambio_id),
    deliveredLabel,
    // Parte de pago: tomamos el equipo del cliente sin entregarle otro.
    sinEntrega: Boolean(hasSwap && sinEntrega),
    destino: hasSwap ? destino : "",
    destinoLabel: hasSwap ? swapDestinoLabel(destino) : "",
    destinoResumen: hasSwap ? swapDestinoResumen(destino) : "",
    // El destino ya pasó el equipo a nuestro nombre; lo consumen las pantallas que
    // muestran de quién es el equipo sin volver a razonar sobre la lista de destinos.
    esPropiedadSepid: Boolean(hasSwap && SWAP_DESTINOS_PATRIMONIALES.includes(destino)),
    canRegister: Boolean(canSwap && !blockedReason),
    canUndo: Boolean(canSwap && !blockedReason && hasSwap),
    blockedReason,
  };
}

/** Etiqueta del ítem de menú, según lo que la OS ya tenga registrado. */
export function swapActionLabel(state = {}) {
  if (!state?.hasSwap) return "Cambio de equipo / parte de pago";
  return state.sinEntrega ? "Ver / editar parte de pago" : "Ver / editar cambio de equipo";
}

/** Texto corto para mostrar el cambio en una tarjeta o en el modal de salida. */
export function describeSwapSummary(row = {}) {
  const { hasSwap, deliveredLabel, destinoResumen, sinEntrega } = getDeviceSwapState(row, {
    canSwap: true,
  });
  if (!hasSwap) return "";
  const base = sinEntrega
    ? "El equipo queda en SEPID como parte de pago; no se entrega ninguno a cambio"
    : `Se entrega ${deliveredLabel}; el equipo recibido queda en SEPID`;
  return destinoResumen ? `${base} (${destinoResumen}).` : `${base}.`;
}

/**
 * ¿La serie tipeada al entregar coincide con el equipo del cambio?
 *
 * El snapshot puede venir como "NS | MG 0001": alcanza con que coincida una de las partes.
 * Espejo de la verificación de `EntregarIngresoView`.
 */
export function swapConfirmMatches(typed, deliveredLabel) {
  const value = text(typed).toUpperCase();
  if (!value) return false;
  const partes = text(deliveredLabel)
    .split("|")
    .map((p) => p.trim().toUpperCase())
    .filter(Boolean);
  return partes.includes(value);
}
